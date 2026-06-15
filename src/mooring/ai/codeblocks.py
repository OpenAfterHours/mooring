"""Extract fenced code blocks from a streamed assistant reply.

The one-shot ``copilot._extract_code`` only grabs the first block; a chat reply
can contain several, so the chat renders each as a separately-applyable cell.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_PY = {"python", "py", ""}


def extract_code_blocks(text: str) -> list[str]:
    """Return fenced code blocks in order. Prefer python/unlabelled fences; if
    there are none, fall back to every fenced block. Empty list if none."""
    matches = [(lang.lower(), body.strip("\n")) for lang, body in _FENCE.findall(text or "")]
    python = [code for lang, code in matches if lang in _PY and code.strip()]
    if python:
        return python
    return [code for _, code in matches if code.strip()]
