"""``python -m mooring`` — the invocation of last resort.

The console-script shim (``mooring``) is the normal entry point, but it is not always
resolvable to a STABLE path: under ``uvx`` it lives in an ephemeral cache uv may garbage
-collect. :func:`mooring.schedule_os.resolve_command` falls back to ``-m mooring`` when the
interpreter itself is stable, which is what lets a background refresh be registered from a
plain ``pip install`` into a real environment.
"""

from __future__ import annotations

import sys

from mooring.cli import main

if __name__ == "__main__":
    sys.exit(main())
