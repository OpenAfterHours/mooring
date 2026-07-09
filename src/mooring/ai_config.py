"""The AI/PII configuration schema, in one place.

:class:`AiConfig` and its nested :class:`PiiConfig` are the single source of truth
for the copilot's settings and their defaults — including the privacy guard. The
parser :func:`load_ai_config` maps the ``[ai]`` / ``[ai.pii]`` TOML tables and the
``MOORING_AI_*`` env overrides onto them, so adding a knob is a one-place change
(a field here + its parse line) instead of a field on a god-dataclass plus a
separate loader line elsewhere.

:class:`AppConfig` keeps flat ``ai_*`` / ``ai_pii_*`` read-only properties that
forward here, so existing readers are unchanged; the value of the nested form is
that the whole :class:`PiiConfig` travels to the chat session as ONE object (see
``AIProvider.open_chat``), so a guard field can never be silently dropped on the
way.

Pure stdlib: this module imports nothing from the rest of ``mooring`` (it sits
below ``config``, which imports it), so there is no import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# Default NER name-detection model: a SAFETENSORS build loaded as its bf16 variant
# (no pickle), pinned to a commit for reproducibility.
_DEFAULT_NAME_MODEL = "gliner-community/gliner_small-v2.5"
_DEFAULT_NAME_REVISION = "f227d3cd637bd4e6757ae143935316d062393341"


@dataclass(frozen=True)
class PiiConfig:
    """The outbound-PII guard settings. ``enabled`` is the master switch; the
    ``name_*`` fields configure the optional local NER name pass."""

    enabled: bool = False
    block_prompt: bool = True
    scan_source: bool = True
    names: bool = False
    # Which NER backend detects names/orgs. "auto" (default) auto-selects at runtime:
    # the offline "spacy" backend when the `pii-spacy` extra + its model are present,
    # else "gliner" (which downloads from Hugging Face) — so installing an extra is
    # enough, no config edit. "gliner"/"spacy" pin a backend explicitly. See
    # mooring.ai.ner.resolve_backend. The name_model/revision/variant below are
    # GLiNER's; for spaCy, name_model is empty (the vendored model) or a model name/path.
    name_backend: str = "auto"
    name_model: str = _DEFAULT_NAME_MODEL
    name_revision: str = _DEFAULT_NAME_REVISION
    name_variant: str = "bf16"
    name_labels: tuple[str, ...] = ("person", "name")
    name_threshold: float = 0.7


@dataclass(frozen=True)
class BatchConfig:
    """Unattended batch notebook generation (the orchestrator). Default OFF.

    ``enabled`` is the master switch. ``max_jobs`` and ``max_concurrency`` are the
    load-bearing safety caps: each builder is a full Copilot session (a ~150 MB CLI
    subprocess + an event-loop thread) against ONE account's premium-request quota,
    with no SDK throttle — so the planner runs at most ``max_concurrency`` builders
    at once and refuses a batch larger than ``max_jobs``. ``pii_policy`` is the
    NON-interactive PII decision (there is no human at the prompt to confirm): a
    structured-PII hit in a brief either skips that job (``"block_job"``) or aborts
    the whole batch (``"block_batch"``) — it is NEVER auto-confirmed. There is no
    autonomous-write knob: builders only PROPOSE; a human still Applies each notebook.
    """

    enabled: bool = False
    max_jobs: int = 20
    max_concurrency: int = 3
    job_timeout: int = 180  # wall-clock seconds to build one notebook
    follow_up_turns: int = 0  # bounded extra "keep going" turns to fatten a thin build
    pii_policy: str = "block_job"  # "block_job" | "block_batch"


@dataclass(frozen=True)
class InvestigateConfig:
    """Parallel "investigate" fan-out. Default OFF.

    The copilot may call ``mooring_investigate`` to spawn N READ-ONLY value-blind
    sub-agents that research independent sub-questions CONCURRENTLY, then merge their
    value-free findings back as one tool result so it can propose ONE change — the
    only human gate stays the existing Apply. Each branch is a full model session
    against one account's quota with no throttle, so ``max_concurrency`` mirrors
    :class:`BatchConfig`'s cap philosophy: small on the Copilot provider (a ~150 MB
    CLI subprocess per branch), higher on the OpenAI/LiteLLM (HTTP) path.
    ``max_branches`` hard-caps one investigation; ``branch_timeout`` bounds a branch's
    wall-clock. ``pii_policy`` is the NON-interactive decision (there is no human at a
    sub-agent): a checksum-PII hit in a sub-question skips that branch
    (``"block_branch"``) or the whole investigation (``"block_investigation"``) — it is
    never auto-confirmed. The sub-agents are read-only (no propose/edit tool) and
    ``mooring_investigate`` is never in THEIR toolset, so an investigation cannot recurse.
    """

    enabled: bool = False
    max_branches: int = 8
    max_concurrency: int = 3
    branch_timeout: int = 180  # wall-clock seconds per branch
    pii_policy: str = "block_branch"  # "block_branch" | "block_investigation"


@dataclass(frozen=True)
class AiConfig:
    enabled: bool = True
    provider: str = "copilot"
    model: str = ""
    reasoning_effort: str = ""
    chat_idle_timeout: int = 900
    context: bool = False
    context_dir: str = "context"
    context_max_kb: int = 256
    live_schema: bool = True
    # Read a synced Power BI semantic model (PBIP TMDL): tables, columns,
    # relationships, and measure/calculated-column DAX — authored code, the same
    # class as notebook source, so it defaults ON like the source itself. The
    # extractor is allowlist-based (M partitions/roles/annotations never read) and
    # a synced per-model opt-out lives in the workspace mooring.toml.
    semantic_model: bool = True
    # Read the team's importable .py helper modules under the synced folders and offer
    # the copilot their value-free API SKELETON (signatures + scanned docstrings, NEVER a
    # body) so it can reuse them. Extracted via ast (never imported/executed). OPT-IN, off
    # by default: it is a new egress surface (docstrings are best-effort, like a dictionary
    # description). A synced per-module opt-out lives in the workspace mooring.toml.
    code_index: bool = False
    # Sanitise-and-hold for pasted Python tracebacks (which can embed data values).
    # Default ON: it only ever REMOVES information, and the raw paste is never
    # stored, so there is no send-raw path. Turning it off is a weakening flip.
    traceback_guard: bool = True
    # OpenAI-provider endpoint overrides — VALUE-FREE (a URL and an API version,
    # never the API key, which is resolved locally from env/keyring; see
    # mooring.ai.openai_provider). ``openai_base_url`` points the client at an
    # OpenAI-compatible gateway or an Azure resource; ``openai_api_version`` (when
    # set) selects the AzureOpenAI client for a classic Azure deployment. Both are
    # safe in synced/templated config because neither is a secret.
    openai_base_url: str = ""
    openai_api_version: str = ""
    pii: PiiConfig = field(default_factory=PiiConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    investigate: InvestigateConfig = field(default_factory=InvestigateConfig)


def _as_bool(value: object, default: bool) -> bool:
    """Coerce a TOML bool or a string env override to bool; None keeps default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _str_list(raw: object, default: tuple[str, ...]) -> tuple[str, ...]:
    """Coerce a TOML array (or a single string) to a tuple of strings."""
    if raw is None:
        return default
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)) and all(isinstance(p, str) for p in raw):
        return tuple(p for p in raw if isinstance(p, str))
    raise ValueError("[ai.pii] name_labels must be an array of strings")


def load_ai_config(ai: Mapping, env: Mapping[str, str]) -> AiConfig:
    """Build an :class:`AiConfig` from the merged ``[ai]`` table and env overrides.

    ``ai`` is the ``[ai]`` section (its ``pii`` key is the ``[ai.pii]`` table).
    Env vars (``MOORING_AI_*``) take precedence over the file, matching the rest
    of the config's read path.
    """
    p = ai.get("pii", {})
    if not isinstance(p, Mapping):
        p = {}
    pii = PiiConfig(
        enabled=_as_bool(env.get("MOORING_AI_PII"), _as_bool(p.get("enabled"), False)),
        block_prompt=_as_bool(
            env.get("MOORING_AI_PII_BLOCK_PROMPT"), _as_bool(p.get("block_prompt"), True)
        ),
        scan_source=_as_bool(
            env.get("MOORING_AI_PII_SCAN_SOURCE"), _as_bool(p.get("scan_notebook_source"), True)
        ),
        names=_as_bool(env.get("MOORING_AI_PII_NAMES"), _as_bool(p.get("detect_names"), False)),
        name_backend=env.get("MOORING_AI_PII_NAME_BACKEND", str(p.get("name_backend", "auto"))),
        name_model=env.get(
            "MOORING_AI_PII_NAME_MODEL", str(p.get("name_model", _DEFAULT_NAME_MODEL))
        ),
        name_revision=env.get(
            "MOORING_AI_PII_NAME_REVISION",
            str(p.get("name_model_revision", _DEFAULT_NAME_REVISION)),
        ),
        name_variant=env.get(
            "MOORING_AI_PII_NAME_VARIANT", str(p.get("name_model_variant", "bf16"))
        ),
        name_labels=_str_list(p.get("name_labels"), ("person", "name")),
        name_threshold=float(
            env.get("MOORING_AI_PII_NAME_THRESHOLD", p.get("name_threshold", 0.7))
        ),
    )
    b = ai.get("batch", {})
    if not isinstance(b, Mapping):
        b = {}
    batch = BatchConfig(
        enabled=_as_bool(env.get("MOORING_AI_BATCH"), _as_bool(b.get("enabled"), False)),
        max_jobs=int(env.get("MOORING_AI_BATCH_MAX_JOBS", b.get("max_jobs", 20))),
        max_concurrency=int(
            env.get("MOORING_AI_BATCH_MAX_CONCURRENCY", b.get("max_concurrency", 3))
        ),
        job_timeout=int(env.get("MOORING_AI_BATCH_JOB_TIMEOUT_SEC", b.get("job_timeout_sec", 180))),
        follow_up_turns=int(
            env.get("MOORING_AI_BATCH_FOLLOW_UP_TURNS", b.get("follow_up_turns", 0))
        ),
        pii_policy=env.get("MOORING_AI_BATCH_PII_POLICY", str(b.get("pii_policy", "block_job"))),
    )
    inv = ai.get("investigate", {})
    if not isinstance(inv, Mapping):
        inv = {}
    investigate = InvestigateConfig(
        enabled=_as_bool(env.get("MOORING_AI_INVESTIGATE"), _as_bool(inv.get("enabled"), False)),
        max_branches=int(env.get("MOORING_AI_INVESTIGATE_MAX_BRANCHES", inv.get("max_branches", 8))),
        max_concurrency=int(
            env.get("MOORING_AI_INVESTIGATE_MAX_CONCURRENCY", inv.get("max_concurrency", 3))
        ),
        branch_timeout=int(
            env.get("MOORING_AI_INVESTIGATE_BRANCH_TIMEOUT_SEC", inv.get("branch_timeout_sec", 180))
        ),
        pii_policy=env.get(
            "MOORING_AI_INVESTIGATE_PII_POLICY", str(inv.get("pii_policy", "block_branch"))
        ),
    )
    return AiConfig(
        enabled=_as_bool(env.get("MOORING_AI_ENABLED"), _as_bool(ai.get("enabled"), True)),
        provider=env.get("MOORING_AI_PROVIDER", str(ai.get("provider", "copilot"))),
        model=env.get("MOORING_AI_MODEL", str(ai.get("model", ""))),
        reasoning_effort=env.get(
            "MOORING_AI_REASONING_EFFORT", str(ai.get("reasoning_effort", ""))
        ),
        chat_idle_timeout=int(
            env.get("MOORING_AI_CHAT_IDLE_SEC", ai.get("chat_idle_timeout_sec", 900))
        ),
        context=_as_bool(env.get("MOORING_AI_CONTEXT"), _as_bool(ai.get("context"), False)),
        context_dir=env.get("MOORING_AI_CONTEXT_DIR", str(ai.get("context_dir", "context"))),
        context_max_kb=int(env.get("MOORING_AI_CONTEXT_MAX_KB", ai.get("context_max_kb", 256))),
        live_schema=_as_bool(
            env.get("MOORING_AI_LIVE_SCHEMA"), _as_bool(ai.get("live_schema"), True)
        ),
        semantic_model=_as_bool(
            env.get("MOORING_AI_SEMANTIC_MODEL"), _as_bool(ai.get("semantic_model"), True)
        ),
        code_index=_as_bool(env.get("MOORING_AI_CODE_INDEX"), _as_bool(ai.get("code_index"), False)),
        traceback_guard=_as_bool(
            env.get("MOORING_AI_TRACEBACK_GUARD"), _as_bool(ai.get("traceback_guard"), True)
        ),
        openai_base_url=env.get(
            "MOORING_AI_OPENAI_BASE_URL", str(ai.get("openai_base_url", ""))
        ),
        openai_api_version=env.get(
            "MOORING_AI_OPENAI_API_VERSION", str(ai.get("openai_api_version", ""))
        ),
        pii=pii,
        batch=batch,
        investigate=investigate,
    )
