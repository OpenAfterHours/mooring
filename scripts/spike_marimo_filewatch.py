"""Diagnostic: does writing the .py + `marimo edit --watch` make a cell VISIBLY
appear in a connected tab? (This is what `/api/kernel/run` did NOT do — it ran
the code but never added a cell to the frontend document.)

Connects a websocket as a stand-in browser, writes a new cell into the .py via
marimo's codegen, and checks the socket receives a CreateCell transaction.
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

import websockets

NB = (
    "import marimo\n\n"
    '__generated_with = "0.23.9"\n'
    "app = marimo.App()\n\n\n"
    "@app.cell\n"
    "def _():\n"
    "    seed = 1\n"
    "    return (seed,)\n\n\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)
MARKER = "FILEWATCH_MARKER_CELL"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(port, proc, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"marimo exited (code {proc.returncode})")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("not ready")


def insert_cell(nb_path: Path, code: str) -> None:
    from marimo._ast import codegen
    from marimo._convert.converters import MarimoConvert

    ir = MarimoConvert.from_py(nb_path.read_text("utf-8")).to_ir()
    cell_cls = type(ir.cells[0])
    ir.cells.append(cell_cls(code=code, name="_"))
    nb_path.write_text(codegen.generate_filecontents_from_ir(ir), encoding="utf-8")  # no BOM


async def main() -> int:
    ws_dir = Path(tempfile.mkdtemp(prefix="mooring_fw_"))
    nb = ws_dir / "nb.py"
    nb.write_text(NB, encoding="utf-8")
    port = free_port()
    token = secrets.token_urlsafe(16)
    sid = "fw-session-001"
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
        "--watch",
    ]
    print(f"[*] launching marimo edit --watch on {ws_dir.name}")
    proc = subprocess.Popen(cmd, cwd=str(ws_dir))
    reached = False
    try:
        wait_ready(port, proc)
        ws_url = f"ws://127.0.0.1:{port}/ws?session_id={sid}&file=nb.py&access_token={token}"
        async with websockets.connect(ws_url) as ws:
            for _ in range(6):  # drain kernel-ready
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                op = (json.loads(raw).get("op") or json.loads(raw).get("name")) if raw else None
                if op and "kernel-ready" in str(op):
                    break

            print(f"[*] writing a new cell into {nb.name} via codegen…")
            insert_cell(nb, f'mo.md("{MARKER}")')

            print("[*] watching the socket for the cell to arrive…")
            ops = []
            for _ in range(15):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                    op = msg.get("op") or msg.get("name")
                except Exception:
                    op = "<non-json>"
                ops.append(op)
                if MARKER in raw:  # ty: ignore[unsupported-operator]  # spike: ws.recv() is str|bytes
                    print(f"    tab received op={op!r} carrying the new cell")
                    reached = True
                    break
            print(f"    ops seen: {ops}")
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("\n================ RESULT ================")
    print(f"  cell reached the tab via file-watch: {reached}")
    print("  (PASS means write-.py + --watch makes a cell appear, unlike /api/kernel/run)")
    return 0 if reached else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
