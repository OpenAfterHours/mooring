"""Manual smoke check for the editor launcher (not collected by pytest).

Creates a notebook in a temp workspace, starts the marimo editor subprocess,
verifies it serves HTTP and the ?file= URL responds, then tears it down.
Run with: uv run python tests/manual_editor_check.py
"""

import sys
import tempfile
import urllib.request
from pathlib import Path

from mooring import notebook_template
from mooring.cli import _ensure_child_pythonpath
from mooring.editor import EditorServer


def main() -> int:
    _ensure_child_pythonpath()
    with tempfile.TemporaryDirectory(prefix="mooring-editor-check-") as tmp:
        workspace = Path(tmp)
        rel_path = notebook_template.create(workspace, "smoke test")
        print(f"created {rel_path}")
        server = EditorServer(workspace)
        try:
            server.ensure_started()
            url = server.url_for(rel_path)
            print(f"editor ready on port {server.port}")
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read(2048).decode("utf-8", "replace")
            assert resp.status == 200, f"unexpected status {resp.status}"
            assert "marimo" in body.lower(), "response does not look like marimo"
            print(f"opened {rel_path} -> HTTP {resp.status}, marimo page served")
            print("EDITOR CHECK OK")
            return 0
        finally:
            server.shutdown()
            print("editor shut down")


if __name__ == "__main__":
    sys.exit(main())
