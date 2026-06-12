"""Power BI project (PBIP) artifacts: grouping synced files and launching Desktop.

A PBIP project saved from Power BI Desktop (File -> Save As -> .pbip) is a
small `<name>.pbip` pointer plus sibling `<name>.SemanticModel/` and
`<name>.Report/` folders of text files (TMDL/JSON), which sync file-by-file
like everything else. This module groups those files into one artifact for
the hub UI and opens the pointer in Power BI Desktop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mooring.sync import PULL_STATES, PUSH_STATES, FileState, FileStatus

POINTER_SUFFIX = ".pbip"
ARTIFACT_DIR_SUFFIXES = (".SemanticModel", ".Report")


class PbipLaunchError(Exception):
    pass


@dataclass
class Artifact:
    key: str  # pointer path minus ".pbip", e.g. "reports/Sales"
    name: str  # "Sales"
    members: list[FileStatus]  # includes the .pbip pointer itself

    @property
    def pointer(self) -> str:
        return f"{self.key}{POINTER_SUFFIX}"


def group(files: list[FileStatus]) -> tuple[list[Artifact], set[str]]:
    """Group PBIP project files into artifacts.

    A `.pbip` status (local or remote-only) defines an artifact; any file
    under its `.SemanticModel/` or `.Report/` folders is a member. Artifact
    folders without a pointer stay ungrouped plain files.
    """
    keys = sorted(
        f.path[: -len(POINTER_SUFFIX)] for f in files if f.path.endswith(POINTER_SUFFIX)
    )
    artifacts = []
    member_paths: set[str] = set()
    for key in keys:
        prefixes = tuple(f"{key}{suffix}/" for suffix in ARTIFACT_DIR_SUFFIXES)
        members = [
            f for f in files if f.path == f"{key}{POINTER_SUFFIX}" or f.path.startswith(prefixes)
        ]
        artifacts.append(Artifact(key=key, name=key.rsplit("/", 1)[-1], members=members))
        member_paths.update(f.path for f in members)
    return artifacts, member_paths


def aggregate_state(members: list[FileStatus]) -> str:
    """One badge for a whole artifact. "mixed" means edits are pending in both
    directions (e.g. the model changed locally while the report changed
    remotely) — common with PBIP, and neither plain push nor pull covers it.
    """
    states = {f.state for f in members}
    if FileState.CONFLICT in states:
        return FileState.CONFLICT.value
    has_push = bool(states & PUSH_STATES)
    has_pull = bool(states & PULL_STATES)
    if has_push and has_pull:
        return "mixed"
    if has_push:
        return FileState.MODIFIED.value
    if has_pull:
        return FileState.REMOTE_CHANGED.value
    if FileState.IN_REVIEW in states:
        return FileState.IN_REVIEW.value
    return FileState.SYNCED.value


def launch(path: Path) -> None:
    """Open a .pbip file in Power BI Desktop via the Windows file association."""
    if not hasattr(os, "startfile"):
        raise PbipLaunchError(
            "Opening .pbip files needs Windows with Power BI Desktop installed."
        )
    try:
        os.startfile(path)  # noqa: S606 - user-initiated from the localhost hub
    except OSError as exc:
        raise PbipLaunchError(f"Could not open {path.name}: {exc}") from exc
