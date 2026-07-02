"""Per-workspace settings stored in a SYNCED ``<workspace>/mooring.toml``.

Unlike the user config (``config_store.py``, which is per-machine), this file
lives at the workspace root and rides pull/push/propose like any tracked file —
so a setting written here travels to every teammate who syncs the repo.

It carries these — paths and policy tokens only, never a data value:

- ``[ai] disabled_notebooks`` — the per-notebook AI opt-out, the off switch that
  stops the copilot being opened on a notebook by mistake (e.g. one that handles
  PII). See docs/admins/ai-privacy.md.
- ``[shadow] ignore`` — notebooks whose filename shadows an importable module
  (e.g. polars.py) that the team has acknowledged, so the guard stops warning.
  See :mod:`mooring.shadow`.
- ``[sync] folders`` — extra synced sub-folders (e.g. a uv-workspace package's
  notebooks/) registered when a notebook is created there, so the folder rides
  pull/push for the whole team. ADDITIVE — see :func:`merge_extra_folders`.
- ``[guard] push`` — the push guard's team policy: ``"warn"`` (the default;
  findings need an explicit acknowledge) or ``"block"`` (findings must be fixed
  or pragma-suppressed — no override). See :mod:`mooring.pushguard`.
"""

from __future__ import annotations

import os
import threading
import tomllib
from collections.abc import Iterable
from pathlib import Path

import tomli_w

WORKSPACE_CONFIG_NAME = "mooring.toml"

# Serializes the read-modify-write in set_ai_disabled so two concurrent toggles
# (Starlette runs the endpoint in a threadpool) can't lose-update each other —
# the second os.replace would otherwise clobber the first writer's added entry.
_WRITE_LOCK = threading.Lock()


def config_path(workspace: Path) -> Path:
    return workspace / WORKSPACE_CONFIG_NAME


def normalize_notebook(rel: str) -> str:
    """A notebook's identity key: workspace-relative POSIX path, no surrounding
    slashes or whitespace. Matches ``sync.scan_local`` keys (``as_posix()``) and
    the hub's ``_chat_targets`` notebook_rel, so a path from any caller compares
    equal regardless of a stray backslash."""
    return str(rel).replace("\\", "/").strip().strip("/")


def _read_data(workspace: Path) -> dict:
    """The parsed ``mooring.toml``, or ``{}`` when it is missing OR unparseable.

    Fail-OPEN by design for the READ side (the gate): a half-written or malformed
    shared file must not wedge the hub. A bad commit re-enables AI rather than
    blocking the whole team; the visible file row plus the apply-time gate keep
    that recoverable. The WRITE side (set_ai_disabled) uses _read_data_strict so a
    corrupt file is never silently overwritten.
    """
    path = config_path(workspace)
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _read_data_strict(workspace: Path) -> dict:
    """Parse ``mooring.toml`` WITHOUT failing open — used before a write so a
    corrupt file is never overwritten (which would drop unrelated keys/sections and
    silently break the documented preserve-everything-else guarantee). A missing
    file is still ``{}`` (a fresh write is fine); a parse/IO error propagates so the
    caller can refuse the edit and tell the user to fix the file."""
    path = config_path(workspace)
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text("utf-8"))


def _write_data(workspace: Path, data: dict) -> None:
    """Atomically replace ``mooring.toml`` (the ``config_store.write_user_data``
    idiom: write a sibling temp file, then ``os.replace``)."""
    path = config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data), "utf-8")
    os.replace(tmp, path)


def _disabled_list(data: dict) -> set[str]:
    """The normalized opt-out set from already-parsed data (tolerant of a bare
    string or a malformed value)."""
    ai = data.get("ai")
    if not isinstance(ai, dict):
        return set()
    raw = ai.get("disabled_notebooks", [])
    if isinstance(raw, str):  # tolerate a single bare string
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {normalize_notebook(p) for p in raw if str(p).strip()}


def disabled_notebooks(workspace: Path) -> set[str]:
    """The set of notebooks (normalized paths) the copilot is turned OFF for."""
    return _disabled_list(_read_data(workspace))


def is_ai_disabled(workspace: Path, notebook_rel: str) -> bool:
    return normalize_notebook(notebook_rel) in disabled_notebooks(workspace)


def set_ai_disabled(workspace: Path, notebook_rel: str, disabled: bool) -> bool:
    """Add/remove a notebook from the opt-out list, preserving every other key
    and section in ``mooring.toml``. The list is written sorted + deduped (stable
    diffs and sync merges); an emptied list and an emptied ``[ai]`` table are
    pruned, and a file left wholly empty is removed (so an enable round-trip never
    leaves a spurious empty file to sync). Returns the notebook's new disabled state.

    Serialized by ``_WRITE_LOCK`` against concurrent toggles. Raises
    ``tomllib.TOMLDecodeError`` (via the strict read) if the file is corrupt, rather
    than overwriting it and dropping unrelated content.
    """
    key = normalize_notebook(notebook_rel)
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        names = _disabled_list(data)
        if disabled:
            names.add(key)
        else:
            names.discard(key)
        ai = data.get("ai")
        if not isinstance(ai, dict):
            ai = {}
        if names:
            ai["disabled_notebooks"] = sorted(names)
            data["ai"] = ai
        else:
            ai.pop("disabled_notebooks", None)
            if ai:
                data["ai"] = ai
            else:
                data.pop("ai", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return disabled


# -- shadow-guard ignore list -------------------------------------------------
# Notebooks whose filename shadows an importable module (e.g. polars.py) that the
# team has acknowledged and wants the guard to stop warning about — the targeted
# off-ramp from the warning, so a deliberate name doesn't push anyone toward the
# blunt per-machine kill switch. Synced like the AI opt-out (travels to teammates);
# PATHS only. See mooring.shadow.


def _shadow_ignore_list(data: dict) -> set[str]:
    """The normalized ignore set from already-parsed data (tolerant of a bare string
    or a malformed value)."""
    shadow = data.get("shadow")
    if not isinstance(shadow, dict):
        return set()
    raw = shadow.get("ignore", [])
    if isinstance(raw, str):  # tolerate a single bare string
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {normalize_notebook(p) for p in raw if str(p).strip()}


def shadow_ignored(workspace: Path) -> set[str]:
    """Notebooks (normalized paths) the shadow guard should stay quiet about. Fails
    open like the rest of the read side (a malformed file → no ignores)."""
    return _shadow_ignore_list(_read_data(workspace))


def set_shadow_ignored(workspace: Path, notebook_rel: str, ignored: bool) -> bool:
    """Add/remove a notebook from the shadow-guard ignore list, preserving every
    other key and section in ``mooring.toml`` (the :func:`set_ai_disabled` idiom:
    strict read, sorted+deduped write, prune-empty, atomic replace, serialized by
    ``_WRITE_LOCK``). Returns the notebook's new ignored state."""
    key = normalize_notebook(notebook_rel)
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        names = _shadow_ignore_list(data)
        if ignored:
            names.add(key)
        else:
            names.discard(key)
        shadow = data.get("shadow")
        if not isinstance(shadow, dict):
            shadow = {}
        if names:
            shadow["ignore"] = sorted(names)
            data["shadow"] = shadow
        else:
            shadow.pop("ignore", None)
            if shadow:
                data["shadow"] = shadow
            else:
                data.pop("shadow", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return ignored


# -- push-guard policy ----------------------------------------------------------


def guard_mode(workspace: Path) -> str:
    """The team's push-guard policy from ``[guard] push``: ``"warn"`` (default)
    or ``"block"``. Fails open to ``"warn"`` like the rest of the read side — a
    malformed shared file must never wedge the whole team's pushes, and any
    unknown value is treated as the default rather than an error."""
    guard = _read_data(workspace).get("guard")
    if not isinstance(guard, dict):
        return "warn"
    value = str(guard.get("push", "warn")).strip().lower()
    return value if value == "block" else "warn"


# -- synced notebook folders --------------------------------------------------
# Extra sync folders declared in the SYNCED mooring.toml so a sub-folder (e.g. a
# uv-workspace package's notebooks/) created by one teammate rides pull/push for
# everyone — without each machine adding it to its own [sync] folders. These are
# ADDITIVE: they EXTEND the effective folder set (the union is taken in
# merge_extra_folders), unlike config.toml's [sync] folders, which REPLACES the
# built-in default. Stored under [sync] folders here; only PATHS, never values.


def _folders_list(data: dict) -> list[str]:
    """The normalized, de-duplicated ``[sync] folders`` list from already-parsed data
    (tolerant of a bare string or a malformed value), order preserved."""
    sync = data.get("sync")
    if not isinstance(sync, dict):
        return []
    raw = sync.get("folders", [])
    if isinstance(raw, str):  # tolerate a single bare string
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for p in raw:
        norm = normalize_notebook(p)
        if norm and norm not in out:
            out.append(norm)
    return out


def extra_folders(workspace: Path) -> tuple[str, ...]:
    """The repo's additional synced folders declared in ``mooring.toml`` (``()`` when
    none). Fails open like the rest of the read side (a malformed file → no extras)."""
    return tuple(_folders_list(_read_data(workspace)))


def merge_extra_folders(folders: tuple[str, ...], workspace: Path) -> tuple[str, ...]:
    """``folders`` unioned with the repo's :func:`extra_folders`, order-preserving and
    de-duplicated. The single fold both adapters apply when building the active Config,
    so the synced sub-folders drive every consumer of ``cfg.folders`` (scan/list/sync)."""
    return tuple(dict.fromkeys((*folders, *extra_folders(workspace))))


def add_extra_folder(workspace: Path, folder: str) -> None:
    """Record ``folder`` in ``mooring.toml``'s ``[sync] folders`` if not already present
    (see :func:`add_extra_folders`, the single/­one-folder form)."""
    add_extra_folders(workspace, [folder])


def add_extra_folders(workspace: Path, folders: Iterable[str]) -> None:
    """Record ``folders`` in ``mooring.toml``'s ``[sync] folders`` in ONE atomic write,
    preserving every other key/section (the :func:`set_ai_disabled` idiom: strict read,
    sorted+deduped write, atomic replace, serialized by ``_WRITE_LOCK``). A no-op when
    every folder is empty/already-listed (so adopt never rewrites the file needlessly).
    Raises ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it."""
    keys = [k for k in (normalize_notebook(f) for f in folders) if k]
    if not keys:
        return
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        existing = _folders_list(data)
        merged = list(existing)
        for key in keys:
            if key not in merged:
                merged.append(key)
        if merged == existing:
            return  # nothing new — don't rewrite the file
        sync = data.get("sync")
        if not isinstance(sync, dict):
            sync = {}
        sync["folders"] = sorted(merged)  # stable diffs and sync merges
        data["sync"] = sync
        _write_data(workspace, data)
