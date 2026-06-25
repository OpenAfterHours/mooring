"""Spike: prove live-kernel schema introspection works end-to-end AND value-free.

Closes the one gap the unit tests can't reach: that the FROZEN probe, run in a
real marimo kernel via the production :class:`mooring.ai.introspect.KernelControl`,
actually sees the analyst's dataframes through ``globals()`` and hands their
schema back through the sidecar file — while the data lives OUTSIDE the workspace
and the secret values never appear in the readback.

Flow (mirrors scripts/spike_marimo_http_control.py):
  1. write a parquet full of SECRET values in a dir OUTSIDE the workspace
  2. launch a real headless `marimo edit <ws>` and connect a ws (creates a session)
  3. KernelControl.session_for("nb.py")            -> discovers the session id
  4. run a cell that loads the OUTSIDE parquet into `df` + a derived frame
  5. run the real probe, read the sidecar           -> schema present, SECRET absent

Run with the project venv python. No Copilot / auth needed.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import polars as pl
import websockets

from mooring.ai import introspect

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
NB = 'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\nif __name__ == "__main__":\n    app.run()\n'


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
            urllib.request.urlopen(url, timeout=1)  # noqa: S310
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("marimo not ready in time")


async def main() -> int:
    # 1. data OUTSIDE the workspace
    outside = Path(tempfile.mkdtemp(prefix="mooring_outside_"))
    parquet = outside / "loans.parquet"
    pl.DataFrame(
        {"region": ["EU", "US"], "amount": [10, 20], "note": [SECRET, SECRET + "_2"]}
    ).write_parquet(parquet)

    ws = Path(tempfile.mkdtemp(prefix="mooring_ws_"))
    (ws / "nb.py").write_text(NB, encoding="utf-8")
    port = free_port()
    token = secrets.token_urlsafe(16)
    sid = "spike-introspect-001"

    cmd = [
        sys.executable,
        "-m",
        "marimo",
        "edit",
        str(ws),
        "--headless",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token-password",
        token,
        "--skip-update-check",
        "--watch",
    ]
    print(f"[*] launching marimo on :{port}")
    proc = subprocess.Popen(cmd, cwd=str(ws))
    results: dict[str, bool] = {}
    try:
        wait_ready(port, proc)
        ws_url = f"ws://127.0.0.1:{port}/ws?session_id={sid}&file=nb.py&access_token={token}"
        async with websockets.connect(ws_url) as sock:
            for _ in range(6):  # drain to kernel-ready
                try:
                    raw = await asyncio.wait_for(sock.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                if "kernel-ready" in str(raw):
                    break

            kc = introspect.KernelControl(port, token, timeout=8.0)  # ty: ignore[unresolved-attribute]  # stale spike

            # 3. session discovery via the production client
            discovered = kc.session_for("nb.py")
            results["session_discovered"] = discovered == sid
            print(f"[1] session_for('nb.py') -> {discovered!r} (want {sid!r})")

            # 4. load the OUTSIDE-workspace parquet + a derived frame, like an analyst
            load = (
                "import polars as pl\n"
                f"df = pl.read_parquet(r{str(parquet)!r})\n"
                "eu = df.filter(pl.col('region') == 'EU').select('region', 'amount')\n"
            )
            kc.run(sid, load, cell_id="spike-load")

            # 5. run the real probe until the sidecar reports the frames (kernel is async)
            data: dict = {}
            for _ in range(40):
                out = (
                    Path(tempfile.gettempdir()) / f"mooring-introspect-{secrets.token_hex(6)}.json"
                )
                kc.run(sid, introspect.probe_source(out), cell_id="mooring-introspect")
                data = introspect._poll_read(out, 1.0)
                if any(f.get("name") == "df" for f in data.get("frames", [])):
                    break
                await asyncio.sleep(0.25)

            frames = introspect._parse_frames(data)
            by_name = {f.name: f for f in frames}
            print(f"[2] frames seen: {[f.name for f in frames]}")
            for f in frames:
                print(f"      {f.name}: {[c[0] for c in f.columns]} ({f.n_rows} rows)")

            results["found_loaded_df"] = "df" in by_name
            results["found_derived_frame"] = "eu" in by_name
            results["df_cols_correct"] = "df" in by_name and [
                c[0] for c in by_name["df"].columns
            ] == ["region", "amount", "note"]
            results["df_rowcount"] = "df" in by_name and by_name["df"].n_rows == 2
            blob = json.dumps(data)
            results["no_secret_in_readback"] = SECRET not in blob
            results["no_values_in_readback"] = "EU" not in blob and "US" not in blob

        print("\n================ SPIKE RESULTS ================")
        for k, v in results.items():
            print(f"  {'PASS' if v else 'FAIL'}  {k}")
        ok = all(results.values()) and len(results) == 7
        print(f"\n  OVERALL: {'PASS' if ok else 'NEEDS REVIEW'}")
        return 0 if ok else 1
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
