"""Per-workspace settings stored in a SYNCED ``<workspace>/mooring.toml``.

Unlike the user config (``config_store.py``, which is per-machine), this file
lives at the workspace root and rides pull/push/propose like any tracked file —
so a setting written here travels to every teammate who syncs the repo.

It carries these — paths and policy tokens only, never a data value:

- ``[ai] disabled_notebooks`` — the per-notebook AI opt-out, the off switch that
  stops the copilot being opened on a notebook by mistake (e.g. one that handles
  PII). See docs/admins/ai-privacy.md.
- ``[ai] disabled_semantic_models`` — the per-model AI opt-out for Power BI
  semantic models (PBIP artifact keys, e.g. ``reports/Sales``), so a BI owner
  can fence one model off from the copilot for the whole team.
- ``[shadow] ignore`` — notebooks whose filename shadows an importable module
  (e.g. polars.py) that the team has acknowledged, so the guard stops warning.
  See :mod:`mooring.shadow`.
- ``[sync] folders`` — extra synced sub-folders (e.g. a uv-workspace package's
  notebooks/) registered when a notebook is created there, so the folder rides
  pull/push for the whole team. ADDITIVE — see :func:`merge_extra_folders`.
- ``[guard] push`` — the push guard's team policy: ``"warn"`` (the default;
  findings need an explicit acknowledge) or ``"block"`` (findings must be fixed
  or pragma-suppressed — no override). See :mod:`mooring.pushguard`.
- ``[policy]`` — the admin policy block the CLIENT enforces (a minimum version,
  a push-guard floor, propose-only path globs, AI-off path globs, and pinned
  safety settings). PARSING AND SEMANTICS LIVE IN :mod:`mooring.policy`, which
  owns the tighten-only rule; this module only reads and writes the bytes
  (:func:`read_shared`, :func:`set_policy_key`, :func:`set_policy_setting`).
  ``[ai] disabled_notebooks`` and ``[guard] push`` above are the two settings it
  generalises — both keep working and are folded in, never replaced.
- ``[connections]`` — value-free database connection SHAPE (host/database/
  warehouse/role/…) that travels with the repo so the whole team (and the
  copilot) can reference it by name. A secret-shaped field is REFUSED on write —
  the secret NEVER goes here; it stays local (env var / a sync-excluded local
  file), so it can never ride a push. See :mod:`mooring.connections`.
- ``[datasets]`` — value-free POINTERS to data files that are too big to sync
  (a network share, a URL), so a notebook resolves ``md.path("sales")`` instead
  of hard-coding someone's UNC path. A credential-bearing location (a SAS or
  pre-signed URL) is REFUSED on write; each machine can redirect a name locally.
  See :mod:`mooring.datasets`.
"""

from __future__ import annotations

import os
import threading
import tomllib
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

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


def safe_folder(rel: str) -> str:
    """``rel`` canonicalised as a workspace-relative folder key, or ``""`` when it is
    one we must never hand to the filesystem. Nested folders (``a/b``) are fine — depth
    was never the danger; leaving the workspace is.

    Rejected: the root sentinel (``""``, ``"."``), a ``..`` escape, and an ABSOLUTE path.
    The last one matters because ``Path(workspace) / "C:/x"`` is just ``C:/x`` on Windows,
    so such an entry would make :func:`mooring.sync.synced_paths` walk outside the
    workspace and then raise ``ValueError`` from ``relative_to`` mid-scan — a synced
    ``mooring.toml`` could wedge every teammate's status/pull. ``..`` is caught today only
    by accident (``sync.is_synced_path`` drops any segment starting with ``.``); this is
    the guard that means it.

    ``PureWindowsPath`` is used on EVERY platform on purpose: ``mooring.toml`` is a synced
    file, so an entry authored on Windows must be rejected identically on macOS/Linux,
    where ``"C:/x"`` would otherwise look like an innocent relative folder named ``C:``.
    """
    norm = normalize_notebook(rel)
    if not norm:
        return ""
    win = PureWindowsPath(norm)
    if win.drive or win.root or win.is_absolute():
        return ""
    segs = [s for s in norm.split("/") if s not in ("", ".")]
    if not segs or ".." in segs:
        return ""
    return "/".join(segs)


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
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError):
        # UnicodeDecodeError: a non-UTF-8 file (UTF-16/BOM — a Windows hazard). Fail
        # open like a parse error so a bad encoding can't wedge the whole hub.
        # RecursionError: deeply-nested tables. Whether tomllib recurses until the
        # stack gives out or refuses past its own key-parts limit is a CPython
        # PATCH-level detail, so the same synced file is parseable on one teammate's
        # Python and fatal on another's — leaving it uncaught made a ~6 KB commit a
        # remote kill switch for whoever happened to be on the stricter build.
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


def read_shared(workspace: Path) -> dict | None:
    """The parsed ``mooring.toml`` for a reader that needs SEVERAL sections at once.

    ``{}`` when the file is absent, ``None`` when it exists but cannot be parsed
    (the caller decides how loud to be — :func:`_read_data` fails open to ``{}``
    for the per-section readers). One read, so a caller like :mod:`mooring.policy`
    that consults ``[policy]`` and ``[ai]`` together doesn't parse the file twice.
    """
    path = config_path(workspace)
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError):
        # RecursionError == "this file is unparseable", which is exactly what None
        # means here. See _read_data for why the version-dependence matters.
        return None


def disabled_from(data: dict) -> set[str]:
    """:func:`disabled_notebooks` from already-parsed data (see :func:`read_shared`)."""
    return _disabled_list(data)


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


# -- per-model AI opt-out (Power BI semantic models) ----------------------------
# The semantic-model analogue of the per-notebook opt-out above: a BI owner can
# fence one model off from the copilot for the whole team. Keys are PBIP artifact
# keys (the pointer path minus ".pbip", e.g. "reports/Sales") — PATHS only, never
# a value — normalized like notebook paths so any caller's spelling compares equal.


def _disabled_models_list(data: dict) -> set[str]:
    """The normalized model opt-out set from already-parsed data (tolerant of a
    bare string or a malformed value)."""
    ai = data.get("ai")
    if not isinstance(ai, dict):
        return set()
    raw = ai.get("disabled_semantic_models", [])
    if isinstance(raw, str):  # tolerate a single bare string
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {normalize_notebook(p) for p in raw if str(p).strip()}


def disabled_semantic_models(workspace: Path) -> set[str]:
    """The set of semantic-model keys the copilot is turned OFF for."""
    return _disabled_models_list(_read_data(workspace))


def is_semantic_model_disabled(workspace: Path, model_key: str) -> bool:
    return normalize_notebook(model_key) in disabled_semantic_models(workspace)


def _disabled_code_modules_list(data: dict) -> set[str]:
    """The normalized code-module opt-out set (``[ai] disabled_code_modules``) — dotted
    import paths or workspace-relative .py paths the copilot's code library skips."""
    ai = data.get("ai")
    if not isinstance(ai, dict):
        return set()
    raw = ai.get("disabled_code_modules", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {normalize_notebook(p) for p in raw if str(p).strip()}


def disabled_code_modules(workspace: Path) -> set[str]:
    """The set of helper modules (dotted import path or .py path) the code library is OFF
    for. Fails open like the rest of the read side (a malformed file → no opt-outs)."""
    return _disabled_code_modules_list(_read_data(workspace))


def set_semantic_model_disabled(workspace: Path, model_key: str, disabled: bool) -> bool:
    """Add/remove a semantic model from the opt-out list, preserving every other
    key and section in ``mooring.toml`` (the :func:`set_ai_disabled` idiom: strict
    read, sorted+deduped write, prune-empty, atomic replace, serialized by
    ``_WRITE_LOCK``). Returns the model's new disabled state. Raises
    ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it."""
    key = normalize_notebook(model_key)
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        names = _disabled_models_list(data)
        if disabled:
            names.add(key)
        else:
            names.discard(key)
        ai = data.get("ai")
        if not isinstance(ai, dict):
            ai = {}
        if names:
            ai["disabled_semantic_models"] = sorted(names)
            data["ai"] = ai
        else:
            ai.pop("disabled_semantic_models", None)
            if ai:
                data["ai"] = ai
            else:
                data.pop("ai", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return disabled


# -- team AI context folders (the synced OFFER) --------------------------------
# The value-free MENU of context folders a curator publishes for the repo — the
# multi-folder generalisation of the per-machine [ai] context_dir. Stored SORTED
# (an allowlist has no display order, unlike featured_folders) under [ai]
# context_folders in the SYNCED mooring.toml, so the whole team sees the same offer
# and every offered folder rides pull/push (and thus the pre-push secret scan). Only
# PATHS, never a value. READING them still needs each machine's own [ai] context
# consent bool; a Phase-2 per-user subscription can narrow the read set to a subset
# of this offer (see mooring.app.context_folders). Same trust model as
# featured_folders/disabled_notebooks: anyone in repo mode can push a change.


def _context_folders_list(data: dict) -> list[str]:
    """The normalized, de-duplicated ``[ai] context_folders`` offer from already-parsed
    data (tolerant of a bare string or a malformed value).

    Sanitised with :func:`safe_folder`, exactly like ``[sync] folders`` — the offer is
    folded straight into ``cfg.folders`` (see :func:`mooring.app.context_folders.sync_dirs`),
    so an escaping entry here reaches the sync scan just as one there would. An entry at any
    DEPTH is fine (``reports/finance``); only escapes are dropped. Silently dropping a bad
    entry is the fail-open read side: the next write purges it from the file.
    """
    ai = data.get("ai")
    if not isinstance(ai, dict):
        return []
    raw = ai.get("context_folders", [])
    if isinstance(raw, str):  # tolerate a single bare string
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for p in raw:
        norm = safe_folder(p)
        if norm and norm not in out:
            out.append(norm)
    return out


def context_folders(workspace: Path) -> tuple[str, ...]:
    """The repo's team-published AI context folders (the OFFER), sorted + de-duplicated
    (``()`` when none). Fails open like the rest of the read side (a malformed file → no
    offer)."""
    return tuple(sorted(_context_folders_list(_read_data(workspace))))


def set_context_folder(workspace: Path, folder: str, offered: bool) -> bool:
    """Add/remove ``folder`` in the synced ``[ai] context_folders`` offer, preserving
    every other key and section in ``mooring.toml`` (the :func:`set_ai_disabled` idiom:
    strict read, sorted+deduped write, prune-empty, atomic replace, serialized by
    ``_WRITE_LOCK``). Returns the folder's new offered state. Raises
    ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it.

    ``folder`` may name a folder at any depth (``reports/finance``). Offering one that
    escapes the workspace raises ``ValueError`` — the write-side backstop behind the hub
    route's and the CLI's own checks. WITHDRAWING is never refused, so a bad entry written
    by an older version can always be cleared (and any write purges it regardless, since
    :func:`_context_folders_list` no longer reads it back)."""
    key = safe_folder(folder)
    if offered and not key:
        raise ValueError(f"{folder!r} is not a workspace-relative folder")
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        names = set(_context_folders_list(data))
        if offered:
            names.add(key)
        else:
            names.discard(key)
        ai = data.get("ai")
        if not isinstance(ai, dict):
            ai = {}
        if names:
            ai["context_folders"] = sorted(names)
            data["ai"] = ai
        else:
            ai.pop("context_folders", None)
            if ai:
                data["ai"] = ai
            else:
                data.pop("ai", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return offered


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


# -- admin policy block ---------------------------------------------------------
# The BYTES of the [policy] table only: what the keys MEAN, which values are
# acceptable, and the tighten-only rule all live in mooring.policy (which calls
# these after validating). Kept here so every mooring.toml write goes through the
# one audited idiom — strict read (never overwrite a corrupt shared file),
# prune-empty, atomic replace, serialized by _WRITE_LOCK.


def set_policy_key(workspace: Path, key: str, value: object | None) -> None:
    """Set (or, with ``value=None``, clear) one key in the synced ``[policy]``
    table, preserving every other key and section. An emptied ``[policy]`` table
    is pruned, and a file left wholly empty is removed. Raises
    ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it."""
    _write_section(workspace, ("policy",), key, value)


def set_policy_setting(workspace: Path, key: str, value: bool | None) -> None:
    """Set/clear one entry in the synced ``[policy.settings]`` table. ``key`` is a
    DOTTED setting key (``ai.pii.enabled``); ``tomli_w`` quotes it, and
    ``mooring.policy`` reads both the quoted and the nested spelling back."""
    _write_section(workspace, ("policy", "settings"), key, value)


def _write_section(workspace: Path, section: tuple[str, ...], key: str, value: object | None):
    """Write ``key`` into a (possibly nested) table, pruning empty tables upward."""
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        # Walk down, creating/repairing tables (a non-table on the path is replaced —
        # it could not have been read as a table anyway).
        tables: list[dict] = [data]
        for name in section:
            child = tables[-1].get(name)
            if not isinstance(child, dict):
                child = {}
            tables.append(child)
        leaf = tables[-1]
        if value is None:
            leaf.pop(key, None)
        else:
            leaf[key] = value
        # Re-attach bottom-up, dropping any table left empty.
        for name, parent, child in zip(
            reversed(section), reversed(tables[:-1]), reversed(tables[1:])
        ):
            if child:
                parent[name] = child
            else:
                parent.pop(name, None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)


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
        # Drop root-sentinel / escaping entries ("", ".", "./x", "..", "C:/x") for the SAME
        # reason config._folder_list does: a folder that resolves to the workspace root or
        # outside it makes the local (filesystem) and remote (path-prefix) scans diverge and
        # pull delete files. Loose root files sync on their own rule (sync.in_sync_scope).
        norm = safe_folder(p)
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


# -- featured folders (repo-curated hub display order) --------------------------
# A curator STARS the few top-level folders that matter into the SYNCED mooring.toml
# [hub] featured_folders; the hub then shows those first and folds the rest under a
# "More folders" disclosure for everyone. Display-only and strictly ADDITIVE (an
# absent/empty list = the ordinary render) — it NEVER touches [sync] folders, so what
# actually syncs is unchanged. ORDER is meaningful (display priority), so the list is
# preserved as written, NOT sorted. Only PATHS, never a value.


def _featured_list(data: dict) -> list[str]:
    """The normalized, de-duplicated ``[hub] featured_folders`` list from already-parsed
    data (tolerant of a bare string or a malformed value), ORDER preserved — unlike the
    sync folders, order here is display priority."""
    hub = data.get("hub")
    if not isinstance(hub, dict):
        return []
    raw = hub.get("featured_folders", [])
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


def featured_folders(workspace: Path) -> tuple[str, ...]:
    """The repo's curated, pinned-first hub folders (``()`` when none). Fails open like
    the rest of the read side (a malformed file → no featured folders)."""
    return tuple(_featured_list(_read_data(workspace)))


def set_featured_folder(workspace: Path, folder: str, featured: bool) -> bool:
    """Add/remove ``folder`` in ``[hub] featured_folders``, preserving every other key
    and section in ``mooring.toml`` (the :func:`set_ai_disabled` idiom: strict read,
    prune-empty, atomic replace, serialized by ``_WRITE_LOCK``) — but ORDER-PRESERVING:
    a newly featured folder is APPENDED (display priority), never sorted. A no-op when
    the list wouldn't change. Returns the folder's new featured state. Raises
    ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it."""
    key = normalize_notebook(folder)
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        names = _featured_list(data)
        before = list(names)
        if featured:
            if key and key not in names:
                names.append(key)
        else:
            names = [n for n in names if n != key]
        if names == before:
            return featured  # nothing changed — don't rewrite the shared file
        hub = data.get("hub")
        if not isinstance(hub, dict):
            hub = {}
        if names:
            hub["featured_folders"] = names
            data["hub"] = hub
        else:
            hub.pop("featured_folders", None)
            if hub:
                data["hub"] = hub
            else:
                data.pop("hub", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return featured


# -- connection definitions (value-free shape; the secret stays local) ----------
# A team can define a database connection's SHAPE — host, database, warehouse,
# role, and so on — in the synced mooring.toml so everyone (and the copilot) uses
# the same names, WITHOUT the credential ever travelling. The load-bearing rule:
# a secret-shaped field is REFUSED here on write, and the secret lives only in a
# LOCAL, sync-excluded store (see mooring.connections). Definitions travel; the
# secret does not. Only scalar shape values are kept — never a data value.

# Field-name substrings that mark a value as a SECRET, so it can never be written
# into the synced definitions. Deliberately broad (a false refusal is safe — put
# the field in the local store instead); the exact-name set catches bare fields the
# substrings miss without tripping legit shape names (host/role/warehouse/…).
# NOTE: kept in sync with mooring._connections_runtime (the injected kernel module can't
# import this one); tests/test_connections.py pins that the two lists match, so broadening
# one side without the other fails CI rather than silently disagreeing.
_SECRET_TOKENS = (
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "credential",
    "sas",
    "connectionstring",
    "connection_string",
    "conn_str",
    "dsn",
    "private_key",
    "privatekey",
    "access_key",
    "accesskey",
    "account_key",
    "accountkey",
    "signing",
    "bearer",
    "cert",
    "key",  # substring: catches app_key / signing_key / encryption_key / accountkey
)
_SECRET_EXACT = {"pass", "auth", "pat", "cred", "creds"}


def is_secret_field(name: str) -> bool:
    """Whether a connection field NAME looks like a secret (so it must not be synced).
    Fail-safe: broad matching — over-refusing a field just means it goes to the local
    secret store, which is where any credential belongs anyway."""
    norm = str(name).strip().lower().replace("-", "_")
    return norm in _SECRET_EXACT or any(tok in norm for tok in _SECRET_TOKENS)


# Hoisted to a module constant so mooring._connections_runtime / mooring._datasets_runtime
# (standalone kernel modules that cannot import this one) can mirror it verbatim, and
# tests/test_datasets.py can pin that every detector matches rather than only some.
_SECRET_VALUE_PATTERN = (
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|account[_-]?key|credential|bearer)\s*[=:]"
    r"|[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@"
)
_SECRET_VALUE_RE = None  # compiled lazily in _value_looks_secret


def _value_looks_secret(value) -> bool:
    """Whether a VALUE looks like a credential even under an innocent field name — an
    embedded ``password=…`` / ``token:…`` pair, or a DSN with inline credentials. The
    structural floor at this L1 layer (which cannot import the richer ``ai.secrets``
    scanner); the CLI and the push guard add ``ai.secrets`` on top."""
    import re

    global _SECRET_VALUE_RE
    if _SECRET_VALUE_RE is None:
        _SECRET_VALUE_RE = re.compile(_SECRET_VALUE_PATTERN, re.IGNORECASE)
    return isinstance(value, str) and bool(_SECRET_VALUE_RE.search(value))


# Control characters are stripped from every name key below. A TOML QUOTED key may
# contain them ([connections."wh\ninstructions: ..."]), and these names are placed
# verbatim into the copilot's system context by connections_hint / datasets.copilot_guide;
# scrub_text is a PII scrubber, not an injection guard. A newline in a name has no
# legitimate use, so the cheapest fix is to make one unrepresentable.
_CONTROL_CHARS = "".join(chr(c) for c in (*range(0x00, 0x20), 0x7F))
_NAME_TRANSLATION = str.maketrans("", "", _CONTROL_CHARS)


def normalize_connection_name(name: str) -> str:
    """A connection's identity key: a bare token (letters/digits/``_-.``), LOWER-CASED so
    lookups are case-insensitive, with control characters removed. Used as the
    ``[connections.<name>]`` table key and the env-var / local-secret key."""
    return str(name).translate(_NAME_TRANSLATION).strip().strip("/").replace(" ", "_").lower()


def _scalar(value):
    """A shape value kept in the synced definition — a str/int/float/bool only (a
    nested table or list is not a connection shape field). ``None`` drops it."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value
    return None


def _connections(data: dict) -> dict[str, dict]:
    """The value-free connection shapes from already-parsed data: ``{name: {field:
    scalar}}`` with any secret-shaped field DROPPED (defence in depth on the READ side —
    even a hand-edited secret never reaches a caller or the copilot). Tolerant of a
    malformed table."""
    conns = data.get("connections")
    if not isinstance(conns, dict):
        return {}
    out: dict[str, dict] = {}
    for name, shape in conns.items():
        if not isinstance(shape, dict):
            continue
        clean = {
            k: _scalar(v)
            for k, v in shape.items()
            if not is_secret_field(k) and _scalar(v) is not None
        }
        out[normalize_connection_name(name)] = clean
    return out


def connections(workspace: Path) -> dict[str, dict]:
    """The repo's value-free connection shapes (``{name: {field: value}}``), secret
    fields dropped. Fails open like the rest of the read side (a malformed file → no
    connections)."""
    return _connections(_read_data(workspace))


def connections_raw(workspace: Path) -> dict[str, dict]:
    """The RAW ``[connections]`` table as written (secret-shaped fields NOT dropped) —
    for the pre-flight ``mooring connections check`` only, which must be able to SEE a
    hand-added secret in order to warn about it. Every other consumer uses
    :func:`connections`, which drops them."""
    conns = _read_data(workspace).get("connections")
    return {
        normalize_connection_name(n): dict(s)
        for n, s in conns.items()
        if isinstance(s, dict)
    } if isinstance(conns, dict) else {}


def set_connection(workspace: Path, name: str, fields: dict) -> None:
    """Write a connection's value-free SHAPE to ``mooring.toml``, preserving every other
    key/section (the :func:`set_ai_disabled` idiom). REFUSES a secret-shaped field with a
    ``ValueError`` — the credential must go to the local store (:mod:`mooring.connections`),
    never the synced file. Non-scalar values are dropped. Raises
    ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it."""
    key = normalize_connection_name(name)
    if not key:
        raise ValueError("A connection needs a name.")
    # Refuse a secret by NAME or by VALUE — a credential must never reach the synced file.
    bad = sorted(k for k in fields if is_secret_field(k) or _value_looks_secret(fields[k]))
    if bad:
        raise ValueError(
            "These fields look like secrets and must not be synced: "
            f"{', '.join(bad)}. Store the credential locally with "
            "`mooring connections set-secret` instead."
        )
    clean = {k: _scalar(v) for k, v in fields.items() if _scalar(v) is not None}
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        conns = data.get("connections")
        if not isinstance(conns, dict):
            conns = {}
        # MERGE into the existing shape (the verb is "add"/update), so a second call that
        # sets one more field never silently drops the fields defined earlier.
        existing = conns.get(key)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(clean)
        conns[key] = merged
        data["connections"] = conns
        _write_data(workspace, data)


def remove_connection(workspace: Path, name: str) -> bool:
    """Delete a connection definition, preserving everything else. Returns whether one
    was removed. Prunes an emptied ``[connections]`` table (and a wholly empty file)."""
    key = normalize_connection_name(name)
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        conns = data.get("connections")
        if not isinstance(conns, dict) or key not in conns:
            return False
        del conns[key]
        if conns:
            data["connections"] = conns
        else:
            data.pop("connections", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return True


# -- dataset pointers (a value-free WHERE; the file itself never syncs) ---------
# The file-shaped sibling of [connections]. A push warns at 10 MB and refuses at
# 45 MB, so the big parquet/CSV a team reports off lives OUTSIDE the repo — today as
# a UNC path buried in someone's cell, invisible and unshareable. A pointer names
# that location once, in the synced mooring.toml, so `md.path("sales")` means the
# same file for everyone while each machine can redirect it locally (a different
# drive letter / mount). Only a LOCATION, never a data value — and, exactly as with
# [connections], never a credential: a URL carrying a query string, fragment or
# userinfo is refused here, because that is where every pre-signed / SAS link puts
# its key. See mooring.datasets.

DATASET_KINDS = ("share", "https")

# A URL that carries a query string, a fragment, or userinfo is REFUSED as a dataset
# location. That is a structural rule, not a pattern match, and it is the only shape of
# guard that can be right on first contact with a storage vendor nobody has met yet:
# every pre-signed / SAS link puts its key in exactly those places, but they all name it
# differently (Azure `sig`, S3 `X-Amz-Signature`, Backblaze `Authorization`, SharePoint
# `tempauth`, Dropbox `rlkey`, GCS `key`, Snowflake `st`, …). A denylist has to enumerate
# them; "a pointer is a location, so it needs no query string" does not. The cost of a
# false refusal is one `mooring datasets set-local`, which is what the docs recommend for
# an authenticated source anyway; the cost of a false accept is a live key in git history.
_URL_SCHEME_PATTERN = r"\A[a-z][a-z0-9+.\-]*://"
# Kept as a FLOOR under the structural rule for locations that are not URLs, and for
# pointers written by an older version that the read side must still drop. Mirrored in
# mooring._datasets_runtime; tests/test_datasets.py pins that every detector matches.
_URL_SECRET_PATTERN = (
    r"[?&#/](?:sig|signature|sas|sas[_-]?token|token|access[_-]?token|api[_-]?key|apikey|"
    r"authorization|tempauth|rlkey|auth|key|password|passwd|pwd|secret|credential|"
    r"awsaccesskeyid|x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"x-goog-signature|x-goog-credential)="
)
_URL_SECRET_RE = None  # compiled lazily in location_looks_secret
_URL_SCHEME_RE = None


def location_looks_secret(value) -> bool:
    """Whether a dataset LOCATION carries — or could carry — a credential.

    :func:`is_secret_field` cannot help here: the field is innocently named ``url`` and
    the secret hides in the query string, so the VALUE is what must be inspected. For a
    URL the test is STRUCTURAL — any query string, fragment or userinfo is refused,
    whatever the parameter is called (see ``_URL_SCHEME_PATTERN`` above). Everything else
    falls back to the token floor.

    What this does NOT catch: a credential hidden in a plain path segment
    (``https://host/AKIA…/sales.csv``). That is indistinguishable from a folder name, so
    it is a documented residual — the CLI adds the ``ai.secrets`` scan on top, and the
    rule that actually keeps a team safe is that a pointer names a location while
    authentication happens on each machine.
    """
    import re

    global _URL_SECRET_RE, _URL_SCHEME_RE
    if not isinstance(value, str):
        return False
    if _URL_SCHEME_RE is None:
        _URL_SCHEME_RE = re.compile(_URL_SCHEME_PATTERN, re.IGNORECASE)
    text = value.strip()
    if _URL_SCHEME_RE.match(text):
        rest = text.split("://", 1)[1]
        authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
        if "@" in authority or "?" in rest or "#" in rest:
            return True
    if _URL_SECRET_RE is None:
        _URL_SECRET_RE = re.compile(_URL_SECRET_PATTERN, re.IGNORECASE)
    return bool(_URL_SECRET_RE.search(text))


# A dataset name becomes a DIRECTORY COMPONENT (``.mooring/datasets/cache/<name>/``),
# which a connection name never does — so it needs the path-safety rule of safe_folder on
# top of the token rule. mooring.toml is SYNCED, so without it a pushed
# ``[datasets."c:/users/public/x"]`` (or ``"../../.."``, or a UNC path) would make every
# teammate's kernel write outside the workspace the moment md.path() ran: on Windows
# ``Path(ws) / "c:/x"`` is just ``c:/x``, and ``.mooring/pylib`` is on the kernel's
# sys.path. An ALLOWLIST rather than a denylist for the same reason as the URL rule
# above; it also rejects ``:`` (NTFS alternate data streams) and the reserved device
# names for free.
_DATASET_NAME_PATTERN = r"[a-z0-9][a-z0-9._-]*\Z"
_DATASET_NAME_RE = None
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
     *(f"lpt{i}" for i in range(1, 10))}
)


def normalize_dataset_name(name: str) -> str:
    """A dataset's identity key: a bare, lower-cased, PATH-SAFE token — ``""`` for
    anything that must never reach the filesystem (a separator, a ``..`` escape, an
    absolute or UNC path, a device name, a control character).

    Callers treat ``""`` as "no such dataset": the read side drops the entry, the write
    side refuses it. See ``_DATASET_NAME_PATTERN`` for why this is stricter than
    :func:`normalize_connection_name`."""
    import re

    global _DATASET_NAME_RE
    key = normalize_connection_name(name)
    if not key:
        return ""
    if _DATASET_NAME_RE is None:
        _DATASET_NAME_RE = re.compile(_DATASET_NAME_PATTERN)
    if not _DATASET_NAME_RE.match(key):
        return ""
    return "" if key.split(".", 1)[0] in _RESERVED_DEVICE_NAMES else key


def dataset_location(shape: dict) -> str:
    """The single LOCATION a pointer carries: ``url`` for a ``kind=https`` pointer,
    ``path`` otherwise. ``""`` when the shape has neither — which is what a caller sees
    after :func:`datasets` drops a credential-bearing location, so an unusable pointer
    fails loudly instead of resolving to something surprising."""
    if not isinstance(shape, dict):
        return ""
    kind = str(shape.get("kind", "")).strip().lower()
    order = ("url", "path") if kind == "https" else ("path", "url")
    for field in order:
        value = shape.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _validate_dataset(shape: dict) -> None:
    """Refuse a pointer that could not resolve — or that could resolve to the wrong
    KIND of thing. Raises ``ValueError`` with the fix in the message.

    The scheme check is the load-bearing one: ``urlopen`` happily serves ``file://``,
    so without it a single pushed ``mooring.toml`` would make every teammate's kernel
    read an arbitrary local path under the guise of a download."""
    kind = str(shape.get("kind", "")).strip().lower()
    if kind not in DATASET_KINDS:
        raise ValueError(
            f"A dataset needs kind=<{'|'.join(DATASET_KINDS)}> — 'share' for a file on a "
            "network share or local disk, 'https' for one fetched over http(s)."
        )
    location = dataset_location(shape)
    if not location:
        raise ValueError(
            "A dataset needs a location: path=<file or UNC path> for kind=share, "
            "url=<https://…> for kind=https."
        )
    lowered = location.lower()
    if kind == "https" and not lowered.startswith(("http://", "https://")):
        raise ValueError("A kind=https dataset's url= must start with http:// or https://.")
    if kind == "share" and lowered.startswith(("http://", "https://")):
        raise ValueError("That location is a URL — define it with kind=https url=… instead.")


def _datasets(data: dict) -> dict[str, dict]:
    """The value-free dataset pointers from already-parsed data, with any unsafe NAME,
    secret-shaped FIELD and credential-bearing LOCATION dropped (defence in depth on the
    READ side, mirroring :func:`_connections`) — so a hand-edited SAS URL or a traversing
    name never reaches a caller, the kernel, or the copilot. Tolerant of a malformed
    table."""
    sets = data.get("datasets")
    if not isinstance(sets, dict):
        return {}
    out: dict[str, dict] = {}
    for name, shape in sets.items():
        key = normalize_dataset_name(name)
        if not key or not isinstance(shape, dict):
            continue  # an unsafe name is not a dataset — `datasets check` reports it
        out[key] = {
            k: _scalar(v)
            for k, v in shape.items()
            if not is_secret_field(k)
            and _scalar(v) is not None
            and not _value_looks_secret(v)
            and not location_looks_secret(v)
        }
    return out


def datasets(workspace: Path) -> dict[str, dict]:
    """The repo's value-free dataset pointers (``{name: {field: value}}``). Fails open
    like the rest of the read side (a malformed file → no datasets)."""
    return _datasets(_read_data(workspace))


def datasets_raw(workspace: Path) -> dict[str, dict]:
    """The RAW ``[datasets]`` table as written (nothing dropped) — for the pre-flight
    ``mooring datasets check`` only, which must be able to SEE a hand-added credential, or
    a name that traverses, in order to warn about it. Every other consumer uses
    :func:`datasets`.

    Keyed by the name AS WRITTEN (control characters removed so it is safe to print),
    NOT by :func:`normalize_dataset_name` — which returns ``""`` for exactly the names
    this command exists to report, and would collide them all into one entry."""
    sets = _read_data(workspace).get("datasets")
    if not isinstance(sets, dict):
        return {}
    return {
        str(n).translate(_NAME_TRANSLATION).strip(): dict(s)
        for n, s in sets.items()
        if isinstance(s, dict)
    }


def set_dataset(workspace: Path, name: str, fields: dict) -> None:
    """Write a dataset POINTER to ``mooring.toml``, preserving every other key/section
    (the :func:`set_ai_disabled` idiom) and MERGING into an existing pointer like
    :func:`set_connection`. Raises ``ValueError`` for a credential-shaped field or
    location, or a pointer that could not resolve; ``tomllib.TOMLDecodeError`` on a
    corrupt file rather than overwriting it."""
    key = normalize_dataset_name(name)
    if not key:
        raise ValueError(
            f"{name!r} is not a usable dataset name. A name becomes a folder under "
            ".mooring, so it must be a bare token: a letter or digit followed by letters, "
            "digits, dot, underscore or hyphen (e.g. sales, sales_2024, fx.rates)."
        )
    bad = sorted(
        k
        for k, v in fields.items()
        if is_secret_field(k) or _value_looks_secret(v) or location_looks_secret(v)
    )
    if bad:
        raise ValueError(
            "These fields carry — or could carry — a credential and must not be synced: "
            f"{', '.join(bad)}. A pointer carries only a LOCATION, so a URL may not have a "
            "query string, a fragment or embedded credentials. Mount the share (or sign in "
            "to it) on each machine, and use `mooring datasets set-local` to point the name "
            "at an already-authenticated path where it differs."
        )
    clean = {k: _scalar(v) for k, v in fields.items() if _scalar(v) is not None}
    if isinstance(clean.get("kind"), str):
        clean["kind"] = clean["kind"].strip().lower()
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        sets = data.get("datasets")
        if not isinstance(sets, dict):
            sets = {}
        existing = sets.get(key)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(clean)
        _validate_dataset(merged)  # validate the MERGED pointer — that is what will resolve
        sets[key] = merged
        data["datasets"] = sets
        _write_data(workspace, data)


def remove_dataset(workspace: Path, name: str) -> bool:
    """Delete a dataset pointer, preserving everything else. Returns whether one was
    removed. Prunes an emptied ``[datasets]`` table (and a wholly empty file).

    Falls back to the name AS WRITTEN when the normalised form doesn't match, so an entry
    with an UNSAFE name — the one case a user most needs to delete — can still be removed.
    Withdrawing is never refused, the :func:`set_context_folder` rule."""
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        sets = data.get("datasets")
        if not isinstance(sets, dict):
            return False
        key = next(
            (
                candidate
                for candidate in (normalize_dataset_name(name), str(name).strip(), str(name))
                if candidate and candidate in sets
            ),
            "",
        )
        if not key:
            return False
        del sets[key]
        if sets:
            data["datasets"] = sets
        else:
            data.pop("datasets", None)
        if data:
            _write_data(workspace, data)
        else:
            config_path(workspace).unlink(missing_ok=True)
    return True


def connections_hint(workspace: Path) -> str:
    """A value-free, one-block capability note for the AI system context: the connection
    NAMES and their shape FIELDS (never a value or a secret), so the copilot can write
    connection code that references them via ``mooring_connections``. ``""`` when none."""
    conns = connections(workspace)
    if not conns:
        return ""
    lines = ["CONNECTIONS (value-free shapes; the copilot NEVER sees the secret):"]
    for name in sorted(conns):
        fields = ", ".join(f"{k}={v}" for k, v in sorted(conns[name].items()))
        lines.append(f"- {name}: {fields}" if fields else f"- {name}")
    lines.append(
        "To use one, propose a cell that calls `import mooring_connections as mc; "
        'c = mc.get("<name>")` — it merges this shape with the LOCAL secret (env var or a '
        "sync-excluded local file) at runtime. Never inline a credential; reference c.secret."
    )
    return "\n".join(lines)
