"""Phase-0 spike: prove mooring can drive a live marimo edit session over the
authenticated HTTP control API WITHOUT a browser and WITHOUT receiving data.

Proves three things against a real headless `marimo edit` (directory mode):
  1. Session-id discovery     -> POST /api/home/running_notebooks (access_token + skew token)
  2. Cell injection           -> POST /api/document/transaction (Marimo-Session-Id) with a CreateCell
  3. Value-safety on read-back -> HTTP responses + the broadcast carry only `code`, never outputs

Run with the project venv python.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import websockets

NB = """import marimo

__generated_with = "0.23.9"
app = marimo.App()


@app.cell
def _():
    seed = 1 + 1
    return (seed,)


if __name__ == "__main__":
    app.run()
"""

INJECT_MARKER = "INJECTED_BY_MOORING_SPIKE"
INJECT_CODE = f"spike_result = 41 + 1  # {INJECT_MARKER}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"marimo exited early (code {proc.returncode})")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # any HTTP response means it's up
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("marimo not ready in time")


def extract_server_token(html: str) -> str | None:
    for pat in (
        r'serverToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'serverToken["\']?\s*[:=]\s*([A-Za-z0-9_\-]+)',
        r'server[_-]?token["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-]+)',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


async def main() -> int:
    ws_dir = Path(tempfile.mkdtemp(prefix="mooring_spike_"))
    (ws_dir / "nb.py").write_text(NB, encoding="utf-8")  # plain UTF-8, no BOM
    port = free_port()
    token = secrets.token_urlsafe(16)
    sid = "spike-session-001"

    cmd = [
        sys.executable,
        "-m",
        "marimo",
        "edit",
        str(ws_dir),
        "--headless",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token-password",
        token,
        "--skip-update-check",
    ]
    print(f"[*] launching: {' '.join(cmd[2:])}")
    proc = subprocess.Popen(cmd, cwd=str(ws_dir))
    results: dict[str, bool] = {}
    try:
        wait_ready(port, proc)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=20, follow_redirects=True) as client:
            # --- scrape skew token from the served HTML ---
            r = await client.get("/", params={"access_token": token})
            server_token = extract_server_token(r.text)
            print(f"[*] GET / -> {r.status_code}; serverToken = {server_token!r}")
            if not server_token:
                snip = r.text[max(0, r.text.lower().find("servertoken") - 40) :][:160]
                print(f"    (could not parse; context: {snip!r})")

            hdr = {
                "Authorization": f"Bearer {token}",
                "Marimo-Server-Token": server_token or "",
            }

            ws_url = f"ws://127.0.0.1:{port}/ws?session_id={sid}&file=nb.py&access_token={token}"
            print(f"[*] connecting ws as session_id={sid!r} ...")
            async with websockets.connect(ws_url) as ws:
                # drain the initial kernel-ready handshake
                for _ in range(6):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        break
                    try:
                        op = json.loads(raw).get("op") or json.loads(raw).get("name")
                    except Exception:
                        op = "<non-json>"
                    print(f"    ws recv op={op}")
                    if op and "kernel-ready" in str(op):
                        break

                async def observe(needle: str, label: str, tries: int = 12, timeout: float = 5.0):
                    """Watch the ws (the 'browser') for a message containing `needle`
                    (e.g. the new cell id). Logs every op seen so we can see what
                    actually propagates."""
                    seen_ops = []
                    for _ in range(tries):
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            break
                        try:
                            msg = json.loads(raw)
                            op = msg.get("op") or msg.get("name")
                        except Exception:
                            op, msg = "<non-json>", None
                        seen_ops.append(op)
                        if needle in raw:  # ty: ignore[unsupported-operator]  # spike: ws.recv() is str|bytes
                            print(f"    {label}: tab received op={op!r} carrying {needle!r}")
                            return True, msg, seen_ops
                    print(f"    {label}: ops seen = {seen_ops}")
                    return False, None, seen_ops

                # === STEP 1: discover the session id programmatically ===
                rn = await client.post("/api/home/running_notebooks", headers=hdr)
                files = rn.json().get("files", []) if rn.status_code == 200 else []
                print(f"\n[1] POST /api/home/running_notebooks -> {rn.status_code}")
                print(f"    {json.dumps(files, indent=2)}")
                discovered = {f.get("sessionId"): f.get("path") for f in files}
                results["discover_session_id"] = sid in discovered
                print(
                    f"    => session_id {sid!r} discovered: {results['discover_session_id']}"
                    f"  (path={discovered.get(sid)!r})"
                )
                results["discover_no_values"] = INJECT_MARKER not in rn.text

                tx_hdr = {**hdr, "Marimo-Session-Id": sid}

                # === STEP 2: /api/document/transaction -- does it reach the tab? (expect NO) ===
                tx_marker = "TX_" + INJECT_MARKER
                create_cell = {
                    "type": "create-cell",
                    "cellId": "spike-cell-tx-01",
                    "code": f"tx_val = 1  # {tx_marker}",
                    "name": "_",
                    "config": {},
                    "before": None,
                    "after": None,
                }
                tx = await client.post(
                    "/api/document/transaction", headers=tx_hdr, json={"changes": [create_cell]}
                )
                print(f"\n[2] POST /api/document/transaction -> {tx.status_code}  body={tx.text!r}")
                results["transaction_http_ok"] = tx.status_code == 200
                tx_reached, _, _ = await observe(
                    "spike-cell-tx-01", "transaction", tries=5, timeout=3.0
                )
                results["transaction_reaches_tab"] = tx_reached
                print(
                    f"    => transaction reached the tab: {tx_reached}  (expected False: originator excluded)"
                )

                # === STEP 3: /api/kernel/run -- does it reach the tab? (expect YES) ===
                run_marker = "RUN_" + INJECT_MARKER
                run = await client.post(
                    "/api/kernel/run",
                    headers=tx_hdr,
                    json={
                        "cellIds": ["spike-cell-run-01"],
                        "codes": [f"run_val = 41 + 1  # {run_marker}"],
                    },
                )
                print(f"\n[3] POST /api/kernel/run -> {run.status_code}  body={run.text!r}")
                results["run_http_ok"] = run.status_code == 200
                run_reached, _, _ = await observe("spike-cell-run-01", "run", tries=14, timeout=5.0)
                results["run_reaches_tab"] = run_reached
                print(
                    f"    => run reached the tab: {run_reached}  (expected True: kernel broadcast)"
                )

                # value-safety: HTTP responses are SuccessResponse only (no outputs to mooring)
                results["http_responses_value_free"] = all(
                    INJECT_MARKER not in r.text and '"output"' not in r.text for r in (rn, tx, run)
                )

        print("\n================ SPIKE RESULTS ================")
        for k, v in results.items():
            print(f"  {'PASS' if v else 'FAIL'}  {k}")
        overall = all(
            results.get(k)
            for k in (
                "discover_session_id",
                "run_http_ok",
                "run_reaches_tab",
                "http_responses_value_free",
            )
        )
        verdict = (
            "PASS - /api/kernel/run is a viable browser-reaching injection channel"
            if overall
            else "NEEDS REVIEW"
        )
        print(
            f"\n  /document/transaction reaches tab: {results.get('transaction_reaches_tab')}"
            f"  (excluded-originator finding)"
        )
        print(f"  OVERALL: {verdict}")
        return 0 if overall else 1
    finally:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
