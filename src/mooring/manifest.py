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
    # The sync scope ([sync] folders / exclude) under which `files` was captured.
    # `files` is only a faithful snapshot of the remote tree *for that scope*, so
    # the head-unchanged fast path in sync._remote_entries may trust it only while
    # the scope is unchanged. None means a pre-scope manifest: the scope is unknown,
    # so callers must refetch the tree rather than trust a possibly-narrower `files`
    # (this is what let a newly-added folder stay invisible to pull).
    scope_folders: tuple[str, ...] | None = None
    scope_exclude: tuple[str, ...] | None = None
    # What the LAST push wrote to cfg.branch: path -> {"prev": sha|None,
    # "new": sha|None}, replaced wholesale on every push. sync.recall() uses it
    # to write the pre-push state back ("recall last push"); prev None = the
    # push created the file, new None = the push deleted it.
    last_push: dict[str, dict] = field(default_factory=dict)
    last_push_branch: str = ""


def manifest_path(workspace: Path) -> Path:
    return workspace / MANIFEST_DIR / MANIFEST_NAME


def load(workspace: Path) -> Manifest:
    path = manifest_path(workspace)
    if not path.is_file():
        return Manifest()
    data = json.loads(path.read_text("utf-8"))
    review = data.get("review") or {}
    scope = data.get("scope") or {}
    last_push = data.get("last_push") or {}
    folders = scope.get("folders")
    exclude = scope.get("exclude")
    return Manifest(
        version=data.get("version", 1),
        branch=data.get("branch", ""),
        head_commit=data.get("head_commit", ""),
        files=dict(data.get("files", {})),
        review_branch=str(review.get("branch", "")),
        review_files=dict(review.get("files", {})),
        scope_folders=tuple(folders) if folders is not None else None,
        scope_exclude=tuple(exclude) if exclude is not None else None,
        last_push=dict(last_push.get("files", {})),
        last_push_branch=str(last_push.get("branch", "")),
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
    if manifest.scope_folders is not None or manifest.scope_exclude is not None:
        payload["scope"] = {
            "folders": list(manifest.scope_folders or ()),
            "exclude": list(manifest.scope_exclude or ()),
        }
    if manifest.last_push:
        payload["last_push"] = {
            "branch": manifest.last_push_branch,
            "files": dict(sorted(manifest.last_push.items())),
        }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), "utf-8")
    os.replace(tmp, path)
