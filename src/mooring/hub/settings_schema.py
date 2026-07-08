"""The editable-settings registry for the in-hub Settings page.

ONE description per knob, in one place. This module is simultaneously:

* the **allowlist** — the hub's settings endpoint writes ONLY keys that resolve to
  a :class:`SettingSpec` here, so a profile write can never inject a dead/unread
  key the way ``mooring config set foo.bar`` can (``config_store.set_value`` writes
  any dotted key verbatim, with no schema anywhere else);
* the **validator** — :func:`coerce` type/range/enum-checks a value before it is
  written;
* the **UI source** — label / group / control / help / sensitivity drive the
  generic renderer in ``static/settings.js``.

Pure stdlib (dataclasses only): it imports nothing from the rest of ``mooring`` so
it stays a leaf the hub adapter can import freely.

Two correctness invariants are pinned by tests (``tests/test_settings.py``):

* ``key`` is the exact TOML dotted key the loader READS — which for several knobs
  differs from the dataclass field name (e.g. ``ai.pii.detect_names`` not
  ``ai.pii.names``, ``ai.chat_idle_timeout_sec`` not ``ai.chat_idle_timeout``).
  Writing the field name would be silently ignored by ``ai_config.load_ai_config``.
* ``accessor`` is the flat ``AppConfig`` property that reads the EFFECTIVE value
  (post-env), so the page shows what the app actually runs with — and a round-trip
  test asserts ``set_value(key) -> load_app_config -> getattr(cfg, accessor)``
  observes the write for every editable key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingSpec:
    key: str  # dotted TOML key the loader reads == write target == identity
    accessor: str  # AppConfig flat property holding the effective (post-env) value
    label: str
    group: str  # one of GROUPS
    type: str  # "bool" | "int" | "float" | "enum" | "str" | "list"
    control: str  # "toggle" | "number" | "select" | "text" | "tags"
    help: str
    default: object
    sensitivity: str = "safe"  # "safe" | "needs_care" | "weakens"
    env_var: str | None = None  # MOORING_* that, when set, masks the file value
    enum_values: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    allow_empty: bool = True  # for str controls: is "" an accepted value
    # Friendly labels for an enum select, positional to enum_values (None = show the
    # raw token). Validation still keys off enum_values, so labels are display-only.
    enum_labels: tuple[str, ...] | None = None
    # When not None, setting the key to this exact value is the privacy-weakening
    # direction and the endpoint requires an explicit confirm; `confirm` is the
    # message shown. The direction differs per knob (PII off vs context/batch on).
    weaken_value: object | None = None
    confirm: str = ""


# Display order of the editable groups (the read-only admin block is separate).
GROUPS: tuple[dict, ...] = (
    {"id": "appearance", "label": "Appearance"},
    {"id": "ai", "label": "AI copilot"},
    {"id": "pii", "label": "PII guard"},
    {"id": "batch", "label": "Batch build"},
    {"id": "sync", "label": "Sync"},
)


EDITABLE: tuple[SettingSpec, ...] = (
    # -- Appearance ----------------------------------------------------------
    SettingSpec(
        key="ui.theme",
        accessor="ui_theme",
        label="Theme",
        group="appearance",
        type="enum",
        control="select",
        enum_values=("light", "dark", "system"),
        enum_labels=("Light", "Dark", "System"),
        default="system",
        env_var="MOORING_UI_THEME",
        help="Appearance of the hub, the AI chat, and your notebooks. "
        "“System” follows your operating system.",
    ),
    # -- AI copilot ----------------------------------------------------------
    SettingSpec(
        key="ai.enabled",
        accessor="ai_enabled",
        label="Enable the AI copilot",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="needs_care",
        env_var="MOORING_AI_ENABLED",
        help="Master switch for the copilot. If your admin set a baseline, this "
        "only changes your machine.",
    ),
    SettingSpec(
        key="ai.provider",
        accessor="ai_provider",
        label="AI backend",
        group="ai",
        type="enum",
        control="select",
        enum_values=("copilot", "openai"),
        enum_labels=("GitHub Copilot", "OpenAI-compatible"),
        default="copilot",
        sensitivity="needs_care",
        env_var="MOORING_AI_PROVIDER",
        help="Which backend answers the copilot. “GitHub Copilot” uses your Copilot "
        "sign-in; “OpenAI-compatible” uses the OpenAI SDK against the base URL below "
        "(OpenAI, Azure, a gateway, or a local server) with a key set on the hub’s AI "
        "card. Switching changes WHERE the value-free schema + notebook source are "
        "sent — it stays value-blind either way, but the destination changes.",
    ),
    SettingSpec(
        key="ai.model",
        accessor="ai_model",
        label="Default model",
        group="ai",
        type="str",
        control="select",  # options fetched from /api/ai/models
        default="",
        env_var="MOORING_AI_MODEL",
        help="Your default model (empty = the provider’s default). You can still "
        "pick a model per chat.",
    ),
    SettingSpec(
        key="ai.reasoning_effort",
        accessor="ai_reasoning_effort",
        label="Default reasoning effort",
        group="ai",
        type="str",
        control="text",
        default="",
        env_var="MOORING_AI_REASONING_EFFORT",
        help="Your default reasoning effort (empty = the model’s default). You can "
        "still pick it per chat.",
    ),
    SettingSpec(
        key="ai.openai_base_url",
        accessor="ai_openai_base_url",
        label="OpenAI base URL",
        group="ai",
        type="str",
        control="text",
        default="",
        sensitivity="needs_care",
        env_var="MOORING_AI_OPENAI_BASE_URL",
        help="Only for the OpenAI-compatible backend: the API base URL. Empty = OpenAI "
        "itself. Point it at an Azure resource, a gateway (LiteLLM/OpenRouter), or a "
        "local server (e.g. http://localhost:11434/v1 for Ollama). A local endpoint "
        "usually needs no API key.",
    ),
    SettingSpec(
        key="ai.openai_api_version",
        accessor="ai_openai_api_version",
        label="OpenAI API version (Azure)",
        group="ai",
        type="str",
        control="text",
        default="",
        sensitivity="needs_care",
        env_var="MOORING_AI_OPENAI_API_VERSION",
        help="Only for Azure OpenAI: the api-version (e.g. 2024-10-21). Setting it "
        "selects the Azure client; leave empty for OpenAI or a non-Azure endpoint.",
    ),
    SettingSpec(
        key="ai.chat_idle_timeout_sec",
        accessor="ai_chat_idle_timeout",
        label="Chat idle timeout (seconds)",
        group="ai",
        type="int",
        control="number",
        minimum=60,
        maximum=86400,
        default=900,
        env_var="MOORING_AI_CHAT_IDLE_SEC",
        help="Close an idle chat session after this many seconds.",
    ),
    SettingSpec(
        key="ai.live_schema",
        accessor="ai_live_schema",
        label="Read live kernel schema",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="needs_care",
        env_var="MOORING_AI_LIVE_SCHEMA",
        help="Read dataframe schemas (names + types only, never values) live from the "
        "running notebook, covering data loaded from outside the workspace. OFF is the "
        "more conservative choice.",
    ),
    SettingSpec(
        key="ai.semantic_model",
        accessor="ai_semantic_model",
        label="Read Power BI semantic models",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="needs_care",
        env_var="MOORING_AI_SEMANTIC_MODEL",
        help="Let the copilot read a synced Power BI semantic model: tables, columns, "
        "relationships, and measure DAX — authored code, never data (partition/source "
        "M expressions and RLS roles are never read). OFF is the more conservative "
        "choice; a per-model opt-out also lives in the synced mooring.toml.",
    ),
    SettingSpec(
        key="ai.traceback_guard",
        accessor="ai_traceback_guard",
        label="Sanitise pasted tracebacks",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="weakens",
        env_var="MOORING_AI_TRACEBACK_GUARD",
        weaken_value=False,
        confirm="Turning the traceback guard OFF sends pasted Python tracebacks to the "
        "assistant RAW. Tracebacks routinely embed data values (KeyError: 'a customer "
        "name', a repr of the offending row), so this re-opens the paste-a-traceback "
        "leak the guard exists to close. Continue?",
        help="Rewrite a pasted traceback into a value-safe form (exception types and "
        "workspace code kept, messages redacted unless provably value-free) and hold it "
        "for a “Send sanitised” confirm. There is deliberately no send-raw option.",
    ),
    SettingSpec(
        key="ai.context",
        accessor="ai_context",
        label="Team context (instructions + data dictionary)",
        group="ai",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="weakens",
        env_var="MOORING_AI_CONTEXT",
        weaken_value=True,
        confirm="Turning team context ON sends your context/ files (instructions.md "
        "verbatim, data-dictionary descriptions) to the assistant — unlike the dataset "
        "schema, this can carry real values, so it is weaker than the value-blind "
        "guarantee. It also makes context/ a SYNCED folder pushed to your whole team. "
        "Run `mooring ai dictionary check` and review the files before enabling. Continue?",
        help="Feed the copilot your workspace’s context/ instructions and data "
        "dictionaries. Off by default — read the warning before enabling.",
    ),
    SettingSpec(
        key="ai.context_dir",
        accessor="ai_context_dir",
        label="Context folder",
        group="ai",
        type="str",
        control="text",
        default="context",
        allow_empty=False,
        sensitivity="needs_care",
        env_var="MOORING_AI_CONTEXT_DIR",
        help="Workspace-relative folder the team context is read from (and synced "
        "from, when team context is on).",
    ),
    SettingSpec(
        key="ai.context_max_kb",
        accessor="ai_context_max_kb",
        label="Context size cap (KB)",
        group="ai",
        type="int",
        control="number",
        minimum=1,
        maximum=4096,
        default=256,
        env_var="MOORING_AI_CONTEXT_MAX_KB",
        help="Maximum instructions text injected per chat (only used when team "
        "context is on).",
    ),
    # -- PII guard -----------------------------------------------------------
    SettingSpec(
        key="ai.pii.enabled",
        accessor="ai_pii",
        label="Outbound PII pre-flight scan",
        group="pii",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="weakens",
        env_var="MOORING_AI_PII",
        weaken_value=False,
        confirm="Turning the PII scan OFF removes your only deterministic check for "
        "well-formed cards / IBANs / NHS numbers / emails / NINOs typed into a prompt "
        "or hard-coded in a cell. The schema-only value-blind design still holds, but "
        "a value a person TYPES would no longer be flagged. Continue?",
        help="Best-effort scan of text leaving for the AI server for well-formed "
        "cards, IBANs, NHS numbers, emails, and NINOs. Defence in depth, not a "
        "guarantee.",
    ),
    SettingSpec(
        key="ai.pii.block_prompt",
        accessor="ai_pii_block_prompt",
        label="Hold the prompt on a PII hit",
        group="pii",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="weakens",
        env_var="MOORING_AI_PII_BLOCK_PROMPT",
        weaken_value=False,
        confirm="Switching to warn-only means a prompt that scans as containing PII is "
        "FORWARDED to the model automatically, with only a passive warning, instead of "
        "being held until you click “send anyway”. Continue?",
        help="On a hit, hold the prompt until you confirm “send anyway”. Off = a "
        "warn-only advisory. (Only acts when the PII scan is on.)",
    ),
    SettingSpec(
        key="ai.pii.scan_notebook_source",
        accessor="ai_pii_scan_source",
        label="Warn on PII-dense notebooks",
        group="pii",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="needs_care",
        env_var="MOORING_AI_PII_SCAN_SOURCE",
        help="Show a one-time banner when a notebook or its schema looks PII-dense. "
        "(Only acts when the PII scan is on.)",
    ),
    SettingSpec(
        key="ai.pii.detect_names",
        accessor="ai_pii_names",
        label="Detect names (local NER)",
        group="pii",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="needs_care",
        env_var="MOORING_AI_PII_NAMES",
        help="Also catch names like “Jane Smith”, using a LOCAL model (needs the "
        "mooring[pii] or mooring[pii-spacy] extra). Scanning stays on this machine. "
        "(Only acts when the PII scan is on.)",
    ),
    SettingSpec(
        key="ai.pii.name_backend",
        accessor="ai_pii_name_backend",
        label="Name-detection backend",
        group="pii",
        type="enum",
        control="select",
        enum_values=("auto", "gliner", "spacy"),
        enum_labels=("Auto", "GLiNER", "spaCy"),
        default="auto",
        sensitivity="needs_care",
        env_var="MOORING_AI_PII_NAME_BACKEND",
        help="“auto” uses the offline spaCy backend when installed, else GLiNER "
        "(downloaded from Hugging Face). Pin “spacy” for air-gapped machines.",
    ),
    SettingSpec(
        key="ai.pii.name_labels",
        accessor="ai_pii_name_labels",
        label="Name labels to flag",
        group="pii",
        type="list",
        control="tags",
        default=["person", "name"],
        help="Zero-shot entity labels to flag — add “organization” to also flag "
        "business names.",
    ),
    SettingSpec(
        key="ai.pii.name_threshold",
        accessor="ai_pii_name_threshold",
        label="Name confidence threshold",
        group="pii",
        type="float",
        control="number",
        minimum=0.0,
        maximum=1.0,
        default=0.7,
        env_var="MOORING_AI_PII_NAME_THRESHOLD",
        help="Confidence cut-off for name detection: raise for fewer/safer hits, "
        "lower for more.",
    ),
    # -- Batch build ---------------------------------------------------------
    SettingSpec(
        key="ai.batch.enabled",
        accessor="ai_batch_enabled",
        label="Enable batch notebook builds",
        group="batch",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="weakens",
        env_var="MOORING_AI_BATCH",
        weaken_value=True,
        confirm="Batch build runs UNATTENDED builders — there is no human at the "
        "prompt, so the interactive “send anyway” PII confirmation is replaced by a "
        "pre-set policy (a hit skips the job or aborts the batch, never auto-confirmed). "
        "It also spends premium AI quota. Builders only PROPOSE; you still apply each "
        "notebook. Continue?",
        help="Build several notebooks at once from a list of briefs. Off by default — "
        "read the warning before enabling.",
    ),
    SettingSpec(
        key="ai.batch.max_jobs",
        accessor="ai_batch_max_jobs",
        label="Max notebooks per batch",
        group="batch",
        type="int",
        control="number",
        minimum=1,
        maximum=100,
        default=20,
        sensitivity="needs_care",
        env_var="MOORING_AI_BATCH_MAX_JOBS",
        help="Refuse a batch larger than this. Each builder is a full AI session "
        "against your quota — raise with care.",
    ),
    SettingSpec(
        key="ai.batch.max_concurrency",
        accessor="ai_batch_max_concurrency",
        label="Max builders at once",
        group="batch",
        type="int",
        control="number",
        minimum=1,
        maximum=16,
        default=3,
        sensitivity="needs_care",
        env_var="MOORING_AI_BATCH_MAX_CONCURRENCY",
        help="How many notebooks build concurrently. There is no throttle — raise "
        "with care.",
    ),
    SettingSpec(
        key="ai.batch.job_timeout_sec",
        accessor="ai_batch_job_timeout",
        label="Per-notebook timeout (seconds)",
        group="batch",
        type="int",
        control="number",
        minimum=30,
        maximum=1800,
        default=180,
        sensitivity="needs_care",
        env_var="MOORING_AI_BATCH_JOB_TIMEOUT_SEC",
        help="Wall-clock seconds to build one notebook before timing out.",
    ),
    SettingSpec(
        key="ai.batch.follow_up_turns",
        accessor="ai_batch_follow_up_turns",
        label="Extra “keep going” turns",
        group="batch",
        type="int",
        control="number",
        minimum=0,
        maximum=10,
        default=0,
        sensitivity="needs_care",
        env_var="MOORING_AI_BATCH_FOLLOW_UP_TURNS",
        help="Bounded extra turns to fatten a thin build. More turns = more quota "
        "per job.",
    ),
    SettingSpec(
        key="ai.batch.pii_policy",
        accessor="ai_batch_pii_policy",
        label="Batch PII policy",
        group="batch",
        type="enum",
        control="select",
        enum_values=("block_job", "block_batch"),
        enum_labels=("Skip that job", "Abort the whole batch"),
        default="block_job",
        sensitivity="needs_care",
        env_var="MOORING_AI_BATCH_PII_POLICY",
        help="What an unattended PII hit does: skip that one job, or abort the whole "
        "batch. Never auto-confirmed.",
    ),
    # -- Sync ----------------------------------------------------------------
    SettingSpec(
        key="sync.warn_file_mb",
        accessor="warn_file_mb",
        label="Warn above (MB)",
        group="sync",
        type="int",
        control="number",
        minimum=1,
        maximum=100,
        default=10,
        help="Warn when pushing a file larger than this.",
    ),
    SettingSpec(
        key="sync.max_file_mb",
        accessor="max_file_mb",
        label="Reject above (MB)",
        group="sync",
        type="int",
        control="number",
        minimum=1,
        maximum=95,
        default=45,
        sensitivity="needs_care",
        help="Hard limit: refuse to push a file larger than this. Raising it too far "
        "risks GitHub’s size limits.",
    ),
    SettingSpec(
        key="review.open_pr",
        accessor="open_pr",
        label="Open the pull request on Propose",
        group="sync",
        type="bool",
        control="toggle",
        default=True,
        help="When you Propose, mooring opens the pull request for you (it appears in "
        "teammates' Reviews inbox). Turn off to only get the compare link, and open the "
        "PR on GitHub yourself.",
    ),
)


_BY_KEY = {spec.key: spec for spec in EDITABLE}


def by_key(key: str) -> SettingSpec | None:
    """The spec for a dotted key, or None when the key is not editable here (the
    allowlist check the settings endpoint relies on)."""
    return _BY_KEY.get(key)


def needs_confirm(spec: SettingSpec, value: object) -> bool:
    """Whether writing ``value`` is the privacy-weakening direction that requires
    an explicit confirmation."""
    return spec.weaken_value is not None and value == spec.weaken_value


def coerce(spec: SettingSpec, value: object) -> object:
    """Validate + normalize a JSON value for ``spec`` into the value to persist.

    The hub receives already-typed JSON, so this is a type/range/enum check rather
    than the CLI's string parser — but the OUTCOME matches ``mooring config set`` so
    a value set here reads back identically. Raises ``ValueError`` (-> HTTP 400) on
    bad input.
    """
    t = spec.type
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{spec.label} must be true or false.")
        return value
    if t in ("int", "float"):
        # Reject bools (a JSON bool is an int subclass) and non-numeric input.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{spec.label} must be a number.")
        num = int(value) if t == "int" else float(value)
        if t == "int" and isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{spec.label} must be a whole number.")
        if spec.minimum is not None and num < spec.minimum:
            raise ValueError(f"{spec.label} must be at least {spec.minimum:g}.")
        if spec.maximum is not None and num > spec.maximum:
            raise ValueError(f"{spec.label} must be at most {spec.maximum:g}.")
        return num
    if t == "enum":
        text = str(value)
        if spec.enum_values and text not in spec.enum_values:
            allowed = ", ".join(spec.enum_values)
            raise ValueError(f"{spec.label} must be one of: {allowed}.")
        return text
    if t == "str":
        if not isinstance(value, str):
            raise ValueError(f"{spec.label} must be text.")
        text = value.strip()
        if not text and not spec.allow_empty:
            raise ValueError(f"{spec.label} cannot be empty.")
        return text
    if t == "list":
        if not isinstance(value, list):
            raise ValueError(f"{spec.label} must be a list.")
        items = [str(v).strip() for v in value]
        items = [v for v in items if v]
        if not items:
            raise ValueError(f"{spec.label} cannot be empty.")
        if len(items) > 20:
            raise ValueError(f"{spec.label} has too many entries (max 20).")
        return items
    raise ValueError(f"Unsupported setting type {t!r}.")  # pragma: no cover
