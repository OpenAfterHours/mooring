"""Admin policy: the synced ``[policy]`` block that the CLIENT enforces.

Because analysts have no git, **mooring is the only road into the shared repo** —
so a gate in this client covers 100% of a team's pushes, a claim no server-side
scanner can make for this audience. Two settings already exploited that position
one-off (``[ai] disabled_notebooks`` and ``[guard] push``); this module
generalises them into one coherent ``[policy]`` table in the SYNCED
``mooring.toml`` (see :mod:`mooring.workspace_config`), which travels with the
repo and is enforced here rather than merely displayed.

What a policy can say
---------------------

.. code-block:: toml

    [policy]
    min_version   = "0.4.29"                  # warn loudly below this
    push_guard    = "block"                   # escalate the secret/PII push guard
    propose_only  = ["reports/**", "data/**"] # never a DIRECT push — Propose only
    ai_off        = ["hr/**", "*.private.py"] # the copilot is off for these paths

    [policy.settings]
    "ai.pii.enabled" = true                   # force the local scan ON
    "ai.context"     = false                  # force team context OFF

The three load-bearing rules
----------------------------

**1. Tighten-only, structurally.** A policy can only ever be MORE restrictive
than local config; it can never weaken a user's own safety settings. That is not
a convention here — it is the only thing the data model can express. Each
governed knob declares ONE ``safe`` value (:data:`KNOBS`), and the parser keeps a
``[policy.settings]`` entry *only when it equals that value*; anything else is
dropped with a reason. The same shape applies to the scale knobs: ``push_guard``
composes with :func:`mooring.workspace_config.guard_mode` by MAX on an ordered
severity scale, and the glob rules (``propose_only`` / ``ai_off``) are pure
additions to a restriction set. There is deliberately **no per-machine override**
and no "policy off" switch: an escape hatch would itself be a weakening.

**2. ``mooring.toml`` is attacker-controlled input.** Anyone with repo write, a
compromised account, or a merged malicious PR can change it. So every rule is
parsed defensively and INDEPENDENTLY: an unknown key, a wrong type, a malformed
or escaping glob degrades to "that one rule is ignored" (recorded in
:attr:`Policy.ignored`, which ``mooring policy show`` prints) — never to a crash,
and never to a weakening. Globs are compiled by :func:`compile_glob`, which
accepts only ``*``/``**``/``?`` (no character classes, so no pattern can produce
an invalid or pathological regex) and routes every pattern through
:func:`mooring.workspace_config.safe_folder` first, so an absolute path or a
``..`` escape can never reach the filesystem or the matcher.

**3. Nothing changes for a repo without a ``[policy]`` block.** Every rule is
absent-by-default and every composition is a no-op on the empty policy, so an
existing repo behaves byte-for-byte as before. ``[ai] disabled_notebooks`` and
``[guard] push`` are FOLDED IN, not replaced: they keep their own readers in
``workspace_config`` and this module unions/maxes on top, so an old repo (and an
old admin's muscle memory) keeps working while new repos get the general form.

Layer: L1, beside ``config`` — it reads ``workspace_config`` (stdlib-pure) and
tightens a ``config.AppConfig``. It imports nothing from the domain, ``ai/``, or
either adapter; the adapters apply :func:`tighten_app_config` where they build
the active config (the ``merge_extra_folders`` idiom) and compose
:func:`make_propose_gate` into ``sync``'s injected ``guard_fn`` seam.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from mooring import config, workspace_config

# The severity scale for the push guard, least to most restrictive. Composition
# is MAX on this list, so a policy can raise "warn" to "block" but never lower it.
GUARD_SCALE: tuple[str, ...] = ("warn", "block")

# Rule names as written in [policy] and accepted by `mooring policy set/unset`.
RULE_MIN_VERSION = "min-version"
RULE_PUSH_GUARD = "push-guard"
RULE_PROPOSE_ONLY = "propose-only"
RULE_AI_OFF = "ai-off"
RULE_SETTING = "setting"
RULES: tuple[str, ...] = (
    RULE_MIN_VERSION, RULE_PUSH_GUARD, RULE_PROPOSE_ONLY, RULE_AI_OFF, RULE_SETTING,
)
# The TOML key each rule is stored under inside [policy].
_RULE_KEYS = {
    RULE_MIN_VERSION: "min_version",
    RULE_PUSH_GUARD: "push_guard",
    RULE_PROPOSE_ONLY: "propose_only",
    RULE_AI_OFF: "ai_off",
}

# Glob limits. Deliberately small: these bound the matcher's work on hostile
# input, and no legitimate policy needs more.
MAX_PATTERNS = 200
MAX_PATTERN_LEN = 200
MAX_PATTERN_SEGMENTS = 20
MAX_DOUBLESTAR = 2  # two "**" is quadratic; more could be made to backtrack


@dataclass(frozen=True)
class Knob:
    """One boolean setting a policy may pin — and the ONE value it may pin it to.

    ``safe`` is the restrictive direction, so the composition in :func:`tighten`
    is one-directional by construction: a policy entry either equals ``safe``
    (and is applied) or does not (and is dropped). There is no third outcome, and
    in particular no way to express "policy sets this to the permissive value".

    ``key`` is the dotted TOML key the loader READS — the same identity
    ``mooring.hub.settings_schema`` uses, so the Settings page can mark the row
    locked. ``path`` is the ``AppConfig`` field path to write, which differs from
    the key for several knobs (``ai.pii.scan_notebook_source`` ->
    ``ai.pii.scan_source``); ``tests/test_policy.py`` pins both correspondences,
    including that ``safe`` never equals a spec's declared ``weaken_value``.
    """

    key: str
    path: tuple[str, ...]
    safe: bool
    label: str


KNOBS: tuple[Knob, ...] = (
    Knob("ai.enabled", ("ai", "enabled"), False, "the AI copilot"),
    Knob("ai.pii.enabled", ("ai", "pii", "enabled"), True, "the outbound PII scan"),
    Knob("ai.pii.block_prompt", ("ai", "pii", "block_prompt"), True, "holding a PII-hit prompt"),
    Knob(
        "ai.pii.scan_notebook_source", ("ai", "pii", "scan_source"), True,
        "the PII-dense notebook warning",
    ),
    Knob("ai.traceback_guard", ("ai", "traceback_guard"), True, "traceback sanitising"),
    Knob("ai.context", ("ai", "context"), False, "team context files"),
    Knob("ai.code_index", ("ai", "code_index"), False, "the team code library"),
    Knob("ai.live_schema", ("ai", "live_schema"), False, "live kernel schema reads"),
    Knob("ai.semantic_model", ("ai", "semantic_model"), False, "Power BI semantic-model reads"),
    Knob("ai.batch.enabled", ("ai", "batch", "enabled"), False, "unattended batch builds"),
)
KNOB_BY_KEY: dict[str, Knob] = {k.key: k for k in KNOBS}


# -- globs ---------------------------------------------------------------------
# A tiny, total glob language: "*" (within one path segment), "**" (any number of
# segments), "?" (one character). Character classes are NOT supported — "[" is
# escaped like any other literal — which is what makes compilation total: there
# is no pattern a hostile mooring.toml can write that fails to compile or that
# backtracks badly. Matching is case-INSENSITIVE on purpose: on Windows the same
# file answers to "Reports/x.py" and "reports/x.py", and for a restriction rule
# matching MORE is the safe direction.


def _collapse(pattern: str) -> str:
    """Fold runs of ``*`` and repeated ``**`` segments to their single form.

    Load-bearing, not tidiness: ``"***"`` would otherwise translate to three
    ADJACENT ``[^/]*`` groups, and adjacent quantifiers over the same character
    class are the classic catastrophic-backtracking shape — a two-line denial of
    service anyone with repo write could commit. Collapsing means the translator
    can never emit adjacent same-language quantifiers within a segment, and at
    most :data:`MAX_DOUBLESTAR` across segments.
    """
    def one(seg: str) -> str:
        if seg and set(seg) == {"*"}:  # an all-stars segment is "*" or "**", never more
            return "*" if len(seg) == 1 else "**"
        return re.sub(r"\*{2,}", "*", seg)  # "**" inside a mixed segment is just "*"

    segs = [one(s) for s in pattern.split("/")]
    out: list[str] = []
    for seg in segs:
        if seg == "**" and out and out[-1] == "**":
            continue
        out.append(seg)
    return "/".join(out)


def _translate(pattern: str) -> str:
    segs = pattern.split("/")
    out: list[str] = []
    for index, seg in enumerate(segs):
        last = index == len(segs) - 1
        if seg == "**":
            # Non-final: swallow zero or more whole segments (the following
            # segment supplies its own text). Final: swallow the rest, if any.
            out.append(r"(?:[^/]+(?:/[^/]+)*)?" if last else r"(?:[^/]+/)*")
        else:
            body = "".join(
                "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in seg
            )
            out.append(body if last else body + "/")
    return "".join(out)


@dataclass(frozen=True)
class Glob:
    """One compiled policy pattern. ``prefix`` is set for a wildcard-free pattern,
    which also matches everything BELOW it (``ai_off = ["hr"]`` covering
    ``hr/pay.py`` is what an admin means; silently matching nothing would be a
    footgun on a rule whose whole job is to restrict)."""

    pattern: str
    regex: re.Pattern[str]
    prefix: str = ""

    def matches(self, rel: str) -> bool:
        norm = workspace_config.normalize_notebook(rel)
        if not norm:
            return False
        if self.regex.fullmatch(norm):
            return True
        return bool(self.prefix) and norm.lower().startswith(self.prefix.lower() + "/")


def compile_glob(pattern: object) -> Glob | None:
    """``pattern`` compiled, or ``None`` when it is unusable — a non-string, an
    empty/absolute/``..``-escaping path (via
    :func:`mooring.workspace_config.safe_folder`, the existing precedent), or one
    past the size/complexity caps. Never raises: a bad pattern in a synced,
    attacker-controlled file must drop that one rule, nothing more."""
    if not isinstance(pattern, str):
        return None
    raw = pattern.strip()
    if not raw or len(raw) > MAX_PATTERN_LEN:
        return None
    # safe_folder is the existing precedent (see workspace_config): it strips
    # leading slashes to repo-relative and REFUSES an absolute path, a drive
    # letter, or a ".." escape — so no pattern can ever address anything outside
    # the workspace, whichever platform authored it.
    norm = _collapse(workspace_config.safe_folder(raw))
    if not norm:
        return None
    segs = norm.split("/")
    if len(segs) > MAX_PATTERN_SEGMENTS or segs.count("**") > MAX_DOUBLESTAR:
        return None
    # A "*" that is not a whole "**" segment must not span a separator; the
    # translator enforces that. Wildcard-free patterns also cover their subtree.
    prefix = norm if not any(ch in norm for ch in "*?") else ""
    try:
        regex = re.compile(_translate(norm), re.IGNORECASE)
    except re.error:  # pragma: no cover - _translate only emits escaped literals
        return None
    return Glob(pattern=norm, regex=regex, prefix=prefix)


@dataclass(frozen=True)
class GlobSet:
    """An ordered set of compiled patterns; ``matches`` is a plain OR, so adding a
    pattern can only ever restrict more."""

    globs: tuple[Glob, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.globs)

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(g.pattern for g in self.globs)

    def matches(self, rel: str) -> bool:
        return any(g.matches(rel) for g in self.globs)

    def matching(self, rel: str) -> str:
        """The first pattern that matches (for an honest, value-free message), or ``""``."""
        for g in self.globs:
            if g.matches(rel):
                return g.pattern
        return ""


def _glob_set(raw: object, name: str, ignored: list[str]) -> GlobSet:
    if raw is None:
        return GlobSet()
    if isinstance(raw, str):  # tolerate a single bare string, like the rest of the file
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        ignored.append(f"[policy] {name}: must be an array of path patterns — ignored")
        return GlobSet()
    out: list[Glob] = []
    seen: set[str] = set()
    for entry in list(raw)[:MAX_PATTERNS]:
        glob = compile_glob(entry)
        if glob is None:
            ignored.append(f"[policy] {name}: {entry!r} is not a usable workspace pattern — ignored")
            continue
        if glob.pattern not in seen:
            seen.add(glob.pattern)
            out.append(glob)
    if len(list(raw)) > MAX_PATTERNS:
        ignored.append(f"[policy] {name}: only the first {MAX_PATTERNS} patterns are used")
    return GlobSet(tuple(out))


# -- versions ------------------------------------------------------------------

_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}")


def _version_tuple(text: object) -> tuple[int, ...] | None:
    """``(0, 4, 29)`` for ``"0.4.29"`` / ``"0.4.29.dev1"``, else ``None``. Only the
    numeric release prefix is compared — a pre-release suffix is ignored rather
    than guessed at."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return tuple(int(p) for p in match.group(0).split("."))


# -- the policy ----------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """The parsed, sanitised ``[policy]`` block. Every field is inert by default,
    so ``Policy()`` (a repo with no policy) composes as a no-op everywhere."""

    min_version: str = ""
    push_guard: str = ""  # "" (says nothing) or a member of GUARD_SCALE
    propose_only: GlobSet = field(default_factory=GlobSet)
    ai_off: GlobSet = field(default_factory=GlobSet)
    # key -> the pinned SAFE value. Only safe values ever land here (see parse).
    settings: Mapping[str, bool] = field(default_factory=dict)
    # Value-free, human reasons a rule was dropped — surfaced by `mooring policy
    # show` and `mooring doctor` so a typo'd policy is visible, not silent.
    ignored: tuple[str, ...] = ()
    # True when mooring.toml exists but could not be parsed at all, so NO rule
    # could be read. Reported loudly (doctor, `policy show`); see the module doc.
    unreadable: bool = False

    @property
    def in_force(self) -> bool:
        return bool(
            self.min_version or self.push_guard or self.propose_only
            or self.ai_off or self.settings
        )

    def locked_value(self, key: str) -> bool | None:
        """The value the policy pins ``key`` to, or ``None`` when it says nothing.
        The single source the Settings page asks before accepting a write."""
        value = self.settings.get(key)
        return value if isinstance(value, bool) else None

    def guard_mode(self, local: str = "warn") -> str:
        """The push-guard mode in force: MAX of the local/legacy mode and the
        policy's, on :data:`GUARD_SCALE`. Tighten-only by construction."""
        modes = [m for m in (local, self.push_guard) if m in GUARD_SCALE]
        return max(modes, key=GUARD_SCALE.index) if modes else GUARD_SCALE[0]

    def version_shortfall(self, current: str) -> str:
        """A one-line warning when ``current`` is below ``min_version``, else ``""``.

        Deliberately advisory: mooring never refuses to run below the floor. A
        blocking floor is a repo-wide self-DoS with no recovery path (you cannot
        push the fix to ``mooring.toml`` if pushing is blocked), and it buys
        little — a client old enough to matter predates ``[policy]`` and would
        not read the floor at all. So this warns loudly, everywhere, and the
        rules that DO bite are the ones a current client can actually enforce.
        """
        want = _version_tuple(self.min_version)
        have = _version_tuple(current)
        if want is None or have is None or have >= want:
            return ""
        return (
            f"Your mooring ({current}) is older than the minimum your team asks for "
            f"({self.min_version}). Update mooring — older versions may not enforce "
            "every team policy."
        )


def parse(data: Mapping | None) -> Policy:
    """Parse the ``[policy]`` table out of an already-loaded ``mooring.toml``.

    Defensive throughout and rule-independent: a bad entry never affects a good
    one, and no input reaches ``eval``/``exec`` or the filesystem. Never raises.
    """
    ignored: list[str] = []
    raw = (data or {}).get("policy")
    if raw is None:
        return Policy()
    if not isinstance(raw, Mapping):
        return Policy(ignored=("[policy] must be a table — the whole block is ignored",))

    known = {"min_version", "push_guard", "propose_only", "ai_off", "settings"}
    for key in raw:
        if str(key) not in known:
            ignored.append(f"[policy] {key!r}: unknown policy key — ignored")

    min_version = ""
    if "min_version" in raw:
        if _version_tuple(raw.get("min_version")) is None:
            ignored.append("[policy] min_version: not a version like \"1.2.3\" — ignored")
        else:
            min_version = str(raw["min_version"]).strip()

    push_guard = ""
    if "push_guard" in raw:
        value = str(raw.get("push_guard", "")).strip().lower()
        if value not in GUARD_SCALE:
            ignored.append(
                f"[policy] push_guard: {raw.get('push_guard')!r} is not one of "
                f"{', '.join(GUARD_SCALE)} — ignored"
            )
        elif value != GUARD_SCALE[0]:
            # "warn" is the floor everyone already has: recording it would be a
            # no-op, and never a way DOWN from a stricter [guard] push.
            push_guard = value

    return Policy(
        min_version=min_version,
        push_guard=push_guard,
        propose_only=_glob_set(raw.get("propose_only"), "propose_only", ignored),
        ai_off=_glob_set(raw.get("ai_off"), "ai_off", ignored),
        settings=_parse_settings(raw.get("settings"), ignored),
        ignored=tuple(ignored),
    )


def _flatten(table: Mapping, prefix: str = "") -> dict[str, object]:
    """``[policy.settings]`` as dotted keys, accepting BOTH TOML spellings: the
    quoted form (``"ai.pii.enabled" = true``, which tomllib keeps as one key) and
    the nested form (``[policy.settings.ai.pii] enabled = true``). An author who
    writes either gets the same policy."""
    out: dict[str, object] = {}
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(_flatten(value, f"{dotted}."))
        else:
            out[dotted] = value
    return out


def _parse_settings(raw: object, ignored: list[str]) -> dict[str, bool]:
    """THE tighten-only composition, in nine lines.

    A ``[policy.settings]`` entry survives only when it names a governed knob AND
    equals that knob's single ``safe`` value. Every other input — an unknown key,
    a non-bool, or the permissive value — is dropped with a reason. There is no
    branch in which a policy makes a setting less restrictive.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        ignored.append("[policy.settings] must be a table — ignored")
        return {}
    out: dict[str, bool] = {}
    for key, value in _flatten(raw).items():
        knob = KNOB_BY_KEY.get(key)
        if knob is None:
            ignored.append(f"[policy.settings] {key!r}: not a policy-governed setting — ignored")
            continue
        if not isinstance(value, bool):
            ignored.append(f"[policy.settings] {key}: must be true or false — ignored")
            continue
        if value is not knob.safe:
            ignored.append(
                f"[policy.settings] {key} = {str(value).lower()}: policy may only make a "
                f"setting stricter (here: {str(knob.safe).lower()}) — ignored"
            )
            continue
        out[key] = knob.safe
    return out


def load(workspace: Path) -> Policy:
    """The repo's policy, read from the synced ``mooring.toml``.

    Follows ``workspace_config``'s fail-open READ posture for an unparseable file
    (a corrupt shared file must not wedge every teammate's hub), but says so:
    ``unreadable`` is set, ``mooring policy show`` prints it, and
    ``mooring doctor`` already FAILs on a mooring.toml that is not valid TOML.
    A file that PARSES degrades per rule, never wholesale.
    """
    data = workspace_config.read_shared(workspace)
    if data is None:
        return Policy(
            unreadable=True,
            ignored=(
                f"{workspace_config.WORKSPACE_CONFIG_NAME} could not be parsed — no team "
                "policy could be read. Fix the shared file (`mooring doctor`).",
            ),
        )
    return parse(data)


# -- composition with the local config -----------------------------------------


def _set_path(obj, path: tuple[str, ...], value):
    """``dataclasses.replace`` down a nested field path (``("ai","pii","enabled")``)."""
    head, rest = path[0], path[1:]
    if not rest:
        return replace(obj, **{head: value})
    return replace(obj, **{head: _set_path(getattr(obj, head), rest, value)})


def tighten(app_cfg: config.AppConfig, pol: Policy) -> config.AppConfig:
    """``app_cfg`` with every policy-pinned knob forced to its SAFE value.

    The one composition point. Because :func:`_parse_settings` only ever stores a
    knob's safe value, this loop cannot loosen anything — the direction is a
    property of the data, not of this code. It runs ABOVE the env layer on
    purpose: ``MOORING_AI_PII=0`` is local config too, and policy outranks it.
    """
    for key in pol.settings:
        knob = KNOB_BY_KEY.get(key)
        if knob is not None:
            app_cfg = _set_path(app_cfg, knob.path, knob.safe)
    return app_cfg


def tighten_app_config(
    app_cfg: config.AppConfig, workspace: Path | None = None
) -> config.AppConfig:
    """:func:`tighten` with the policy read from ``workspace`` (the active repo's
    by default) — the single fold both adapters apply where they build the live
    config, mirroring :func:`mooring.workspace_config.merge_extra_folders`."""
    if workspace is None:
        workspace = app_cfg.config_for(None).workspace()
    return tighten(app_cfg, load(workspace))


def guard_mode(workspace: Path) -> str:
    """The push-guard mode actually in force: the legacy ``[guard] push`` raised by
    ``[policy] push_guard``. With no ``[policy]`` block this is exactly
    :func:`mooring.workspace_config.guard_mode` — the back-compat guarantee."""
    return load(workspace).guard_mode(workspace_config.guard_mode(workspace))


# -- AI gate (generalises [ai] disabled_notebooks) ------------------------------


def ai_gate(workspace: Path) -> Callable[[str], bool]:
    """A predicate "is the copilot off for this path?", reading the shared file ONCE.

    The UNION of the legacy per-notebook opt-out list (``[ai] disabled_notebooks``,
    still authored by the hub's per-row toggle) and the policy's ``ai_off`` globs.
    Union, so turning a policy on can only ever disable the copilot for more
    paths — never re-enable one an existing opt-out had turned off.
    """
    data = workspace_config.read_shared(workspace) or {}
    listed = workspace_config.disabled_from(data)
    globs = parse(data).ai_off

    def blocked(rel: str) -> bool:
        norm = workspace_config.normalize_notebook(rel)
        return bool(norm) and (norm in listed or globs.matches(norm))

    return blocked


def ai_disabled(workspace: Path, notebook_rel: str) -> bool:
    """One-shot :func:`ai_gate` — the policy-aware replacement for
    ``workspace_config.is_ai_disabled`` at the four AI entry points."""
    return ai_gate(workspace)(notebook_rel)


# -- propose-only gate (enforced at sync's guard_fn seam) -----------------------

PROPOSE_ONLY_REASON = "direct push blocked by team policy (propose-only path)"


def make_propose_gate(pol: Policy):
    """Build a ``guard_fn``-shaped callable enforcing ``propose_only`` at
    :func:`mooring.sync.push`'s injected seam — the SAME withhold mechanism the
    push guard rides, so the block happens where the bytes would leave, not in a
    button handler that a second code path could miss.

    Returns ``(gate_fn, blocked)``: ``gate_fn(rel_path, data) -> list[str]``
    (non-empty withholds the file) and ``blocked``: ``rel_path -> reason`` for the
    adapters' messaging. Crucially there is **no token and no acknowledge flag**:
    unlike a push-guard finding, a propose-only block has no override — the road
    is Propose. ``propose`` itself never installs this gate; that is the point.
    """
    blocked: dict[str, str] = {}

    def gate_fn(rel_path: str, _data: bytes) -> list[str]:
        pattern = pol.propose_only.matching(rel_path)
        if not pattern:
            return []
        reason = f"{PROPOSE_ONLY_REASON}: {pattern}"
        blocked[rel_path] = reason
        return [reason]

    return gate_fn, blocked


def compose_guards(*fns):
    """One ``guard_fn`` running each of ``fns`` and concatenating their findings.

    Order matters only for display; every gate always runs, so acknowledging a
    push-guard finding can never skip the policy gate beside it.
    """
    active = [fn for fn in fns if fn is not None]
    if not active:
        return None

    def guard_fn(rel_path: str, data: bytes) -> list[str]:
        out: list[str] = []
        for fn in active:
            out.extend(fn(rel_path, data))
        return out

    return guard_fn


# -- authoring (writes the SYNCED mooring.toml — a push like any other) ---------


def describe(pol: Policy, *, current_version: str = "", local_guard: str = "warn") -> list[str]:
    """Human, value-free lines for ``mooring policy show`` / the hub — WHAT is in
    force and WHY, including the rules that were dropped."""
    lines: list[str] = []
    if pol.unreadable:
        lines.append("  ! the shared mooring.toml could not be parsed — no policy is in force")
    elif not pol.in_force:
        lines.append(
            "  (nothing is enforced by policy — every rule below was ignored)"
            if pol.ignored
            else "  (no [policy] block in this repo — nothing is enforced by policy)"
        )
    if pol.min_version:
        lines.append(f"  minimum mooring version: {pol.min_version} (advisory — warns, never blocks)")
    effective = pol.guard_mode(local_guard)
    if pol.push_guard:
        lines.append(f"  push guard: {effective} (policy raised it from {local_guard})")
    elif pol.in_force:
        lines.append(f"  push guard: {effective} (from [guard] push)")
    for pattern in pol.propose_only.patterns:
        lines.append(f"  propose-only: {pattern} (no direct push — use Propose)")
    for pattern in pol.ai_off.patterns:
        lines.append(f"  AI off: {pattern}")
    for key in sorted(pol.settings):
        knob = KNOB_BY_KEY[key]
        lines.append(f"  setting {key} = {str(knob.safe).lower()} — {knob.label} is locked")
    if current_version:
        shortfall = pol.version_shortfall(current_version)
        if shortfall:
            lines.append(f"  ! {shortfall}")
    for reason in pol.ignored:
        lines.append(f"  ignored: {reason}")
    return lines


def set_rule(workspace: Path, rule: str, values: Iterable[str]) -> str:
    """Author one policy rule in the SYNCED ``mooring.toml``. Returns a human
    confirmation; raises ``ValueError`` on input this module would refuse to
    honour — so an admin never writes a rule that silently does nothing.

    This writes a synced file: the change reaches the team on the next push, and
    rides the push guard like any other file.
    """
    values = [str(v) for v in values]
    if rule == RULE_MIN_VERSION:
        if len(values) != 1 or _version_tuple(values[0]) is None:
            raise ValueError("Give one version, e.g. `mooring policy set min-version 0.4.29`.")
        workspace_config.set_policy_key(workspace, "min_version", values[0].strip())
        return f"Policy: minimum mooring version {values[0].strip()}."
    if rule == RULE_PUSH_GUARD:
        mode = (values[0].strip().lower() if len(values) == 1 else "")
        if mode not in GUARD_SCALE:
            raise ValueError(f"Give one of: {', '.join(GUARD_SCALE)}.")
        if mode == GUARD_SCALE[0]:
            # "warn" is the floor; recording it must never look like a way DOWN
            # from a stricter [guard] push, so clear the policy key instead.
            workspace_config.set_policy_key(workspace, "push_guard", None)
            return "Policy: push guard left at the team default (warn)."
        workspace_config.set_policy_key(workspace, "push_guard", mode)
        return "Policy: push guard set to block — flagged files cannot be pushed at all."
    if rule in (RULE_PROPOSE_ONLY, RULE_AI_OFF):
        if not values:
            raise ValueError("Give at least one path pattern (or use `mooring policy unset`).")
        good = [g.pattern for g in (compile_glob(v) for v in values) if g is not None]
        bad = [v for v, g in zip(values, (compile_glob(v) for v in values)) if g is None]
        if bad:
            raise ValueError(
                f"Not usable workspace patterns: {', '.join(repr(b) for b in bad)}. "
                "Use repo-relative paths with * / ** / ? (no '..', no drive letter)."
            )
        workspace_config.set_policy_key(workspace, _RULE_KEYS[rule], sorted(dict.fromkeys(good)))
        return f"Policy: {rule} = {', '.join(sorted(dict.fromkeys(good)))}."
    if rule == RULE_SETTING:
        if len(values) != 2:
            raise ValueError(
                "Give a setting and a value, e.g. `mooring policy set setting ai.pii.enabled true`."
            )
        key, text = values[0].strip(), values[1].strip().lower()
        knob = KNOB_BY_KEY.get(key)
        if knob is None:
            raise ValueError(
                f"{key!r} is not policy-governed. Governed settings: "
                f"{', '.join(k.key for k in KNOBS)}."
            )
        if text not in ("true", "false"):
            raise ValueError("The value must be true or false.")
        if (text == "true") is not knob.safe:
            raise ValueError(
                f"Policy may only make a setting stricter: {key} can be pinned to "
                f"{str(knob.safe).lower()} only. A policy can never loosen a teammate's setting."
            )
        workspace_config.set_policy_setting(workspace, key, knob.safe)
        return f"Policy: {key} = {str(knob.safe).lower()} — locked for everyone on this repo."
    raise ValueError(f"Unknown policy rule {rule!r}. Known: {', '.join(RULES)}.")


def unset_rule(workspace: Path, rule: str, values: Iterable[str] = ()) -> str:
    """Remove one policy rule (RELAXING the team policy — an ordinary, visible,
    diffable edit to the synced file, unlike a local override, which does not exist)."""
    values = [str(v) for v in values]
    if rule in _RULE_KEYS:
        workspace_config.set_policy_key(workspace, _RULE_KEYS[rule], None)
        return f"Policy: {rule} removed."
    if rule == RULE_SETTING:
        if len(values) != 1:
            raise ValueError("Give the setting key, e.g. `mooring policy unset setting ai.context`.")
        workspace_config.set_policy_setting(workspace, values[0].strip(), None)
        return f"Policy: {values[0].strip()} is no longer locked."
    raise ValueError(f"Unknown policy rule {rule!r}. Known: {', '.join(RULES)}.")
