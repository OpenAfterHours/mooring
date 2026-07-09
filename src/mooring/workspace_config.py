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
- ``[connections]`` — value-free database connection SHAPE (host/database/
  warehouse/role/…) that travels with the repo so the whole team (and the
  copilot) can reference it by name. A secret-shaped field is REFUSED on write —
  the secret NEVER goes here; it stays local (env var / a sync-excluded local
  file), so it can never ride a push. See :mod:`mooring.connections`.
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
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        # UnicodeDecodeError: a non-UTF-8 file (UTF-16/BOM — a Windows hazard). Fail
        # open like a parse error so a bad encoding can't wedge the whole hub.
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
    data (tolerant of a bare string or a malformed value)."""
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
        norm = normalize_notebook(p)
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
    ``tomllib.TOMLDecodeError`` on a corrupt file rather than overwriting it."""
    key = normalize_notebook(folder)
    with _WRITE_LOCK:
        data = _read_data_strict(workspace)
        names = set(_context_folders_list(data))
        if offered and key:
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


_SECRET_VALUE_RE = None  # compiled lazily in _value_looks_secret


def _value_looks_secret(value) -> bool:
    """Whether a VALUE looks like a credential even under an innocent field name — an
    embedded ``password=…`` / ``token:…`` pair, or a DSN with inline credentials. The
    structural floor at this L1 layer (which cannot import the richer ``ai.secrets``
    scanner); the CLI and the push guard add ``ai.secrets`` on top."""
    import re

    global _SECRET_VALUE_RE
    if _SECRET_VALUE_RE is None:
        _SECRET_VALUE_RE = re.compile(
            r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|account[_-]?key|credential|bearer)\s*[=:]"
            r"|[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@",
            re.IGNORECASE,
        )
    return isinstance(value, str) and bool(_SECRET_VALUE_RE.search(value))


def normalize_connection_name(name: str) -> str:
    """A connection's identity key: a bare token (letters/digits/``_-.``), LOWER-CASED so
    lookups are case-insensitive. Used as the ``[connections.<name>]`` table key and the
    env-var / local-secret key."""
    return str(name).strip().strip("/").replace(" ", "_").lower()


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
