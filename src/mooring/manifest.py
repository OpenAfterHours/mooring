"""Local sync state: the base (last-synced) blob SHA for every tracked file.

Stored at <workspace>/.mooring/manifest.json and written atomically so an
interrupted sync never corrupts it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_DIR = ".mooring"
MANIFEST_NAME = "manifest.json"


@dataclass
class Manifest:
    version: int = 1
    branch: str = ""
    head_commit: str = ""
    files: dict[str, str] = field(default_factory=dict)  # repo path -> base blob sha
    # Active proposal (push-for-review) state. review_files maps repo path to
    # the blob sha sent to the review branch; None means a proposed deletion.
    review_branch: str = ""
    review_files: dict[str, str | None] = field(default_factory=dict)


def manifest_path(workspace: Path) -> Path:
    return workspace / MANIFEST_DIR / MANIFEST_NAME


def load(workspace: Path) -> Manifest:
    path = manifest_path(workspace)
    if not path.is_file():
        return Manifest()
    data = json.loads(path.read_text("utf-8"))
    review = data.get("review") or {}
    return Manifest(
        version=data.get("version", 1),
        branch=data.get("branch", ""),
        head_commit=data.get("head_commit", ""),
        files=dict(data.get("files", {})),
        review_branch=str(review.get("branch", "")),
        review_files=dict(review.get("files", {})),
    )


def save(workspace: Path, manifest: Manifest) -> None:
    path = manifest_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": manifest.version,
        "branch": manifest.branch,
        "head_commit": manifest.head_commit,
        "files": dict(sorted(manifest.files.items())),
    }
    if manifest.review_branch:
        payload["review"] = {
            "branch": manifest.review_branch,
            "files": dict(sorted(manifest.review_files.items())),
        }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), "utf-8")
    os.replace(tmp, path)
