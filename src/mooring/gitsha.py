"""Git blob SHA computation, so local change detection needs no downloads.

Git's blob object id is sha1(b"blob <size>\\0" + content). Computing it locally
lets mooring compare workspace files against GitHub tree entries directly.

Python files are normalized to LF before hashing and pushing: Windows editors
flip line endings, and without normalization every notebook would look
modified forever. Data files are hashed byte-faithfully.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def blob_sha(data: bytes) -> str:
    h = hashlib.sha1(b"blob %d\x00" % len(data))
    h.update(data)
    return h.hexdigest()


def is_normalized_path(path: str) -> bool:
    return path.endswith(".py")


def normalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def read_for_push(path: Path, rel_path: str) -> bytes:
    data = path.read_bytes()
    if is_normalized_path(rel_path):
        data = normalize(data)
    return data


def local_blob_sha(path: Path, rel_path: str) -> str:
    return blob_sha(read_for_push(path, rel_path))
