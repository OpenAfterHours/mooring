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


# Shown on a row the repo's admin policy has pinned, and returned as the 409
# message when a write to one is refused. It says WHERE the lock came from and
# which direction it can move — a locked row must never look like a broken
# control, and "policy can only make a setting stricter" is the whole contract.
# The lock itself is decided in mooring.policy (this module stays a pure leaf).
POLICY_LOCK_NOTE = (
    "Locked by your team's policy (the [policy] block in this repo's synced "
    "mooring.toml). A policy can only make a setting stricter, never weaker — change "
    "it with `mooring policy set/unset` and push, or ask whoever maintains the repo."
)


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
        key="ai.trusted_model",
        accessor="ai_trusted_model_preference",
        label="Default customer-data model",
        group="ai",
        type="str",
        control="select",  # exact options injected from the managed allowlist
        default="",
        help="Your default for customer-data conversations. Only models explicitly "
        "approved by your administrator are offered or accepted; this preference "
        "cannot make another model trusted. It applies to newly opened chats only.",
    ),
    SettingSpec(
        key="ai.routing_preference",
        accessor="ai_routing_preference",
        label="Default AI routing",
        group="ai",
        type="enum",
        control="select",
        enum_values=("auto", "trusted"),
        enum_labels=("Automatic", "Always use approved"),
        default="auto",
        help="Automatic lets the approved checker choose the route. Always use approved "
        "can only move conversations upward to the customer-data service; it cannot "
        "force customer information to a general model or bypass a block. This default "
        "applies to newly opened chats only.",
    ),
    SettingSpec(
        key="ai.routing.enabled",
        accessor="ai_routing_local_enabled",
        label="Self-configured customer-data routing",
        group="ai",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="weakens",
        # MOORING_AI_ROUTING is authoritative whenever it is PRESENT — truthy means a
        # managed profile is live, falsy means the launcher switched routing off — so
        # its presence is exactly the condition under which all six rows below are
        # inert. See mooring.ai_config.RoutingConfig.
        env_var="MOORING_AI_ROUTING",
        weaken_value=True,
        confirm=(
            "This lets customer information leave this machine. Mooring normally holds "
            "it back; routing sends it to the endpoint you configure here, judged by the "
            "classifier you nominate. Nobody has approved that endpoint on your behalf, "
            "so chats will say “Self-configured”, never “approved”. Turn it on only if "
            "you are sure the endpoint is a legitimate destination for this data."
        ),
        help="Set up customer-data routing yourself when no administrator has done it "
        "for you. A managed profile in the environment always wins, and your team's "
        "policy can switch this off. Fill in every field below and store an API key; "
        "an incomplete profile stays unused.",
    ),
    SettingSpec(
        key="ai.routing.base_url",
        accessor="ai_routing_local_base_url",
        label="Customer-data endpoint",
        group="ai",
        type="str",
        control="text",
        default="",
        sensitivity="needs_care",
        env_var="MOORING_AI_ROUTING",
        help="The HTTPS OpenAI-compatible or Azure endpoint that may receive customer "
        "information. Must be an explicit https:// URL with no credentials, query "
        "string, or fragment. This is a different endpoint from the general “OpenAI "
        "base URL” above and uses its own API key.",
    ),
    SettingSpec(
        key="ai.routing.api_version",
        accessor="ai_routing_local_api_version",
        label="Customer-data API version (Azure)",
        group="ai",
        type="str",
        control="text",
        default="",
        sensitivity="needs_care",
        env_var="MOORING_AI_ROUTING",
        help="Only for Azure: the api-version of the endpoint above (e.g. 2024-10-21). "
        "Leave empty for a standard OpenAI-compatible endpoint.",
    ),
    SettingSpec(
        key="ai.routing.classifier_model",
        accessor="ai_routing_local_classifier_model",
        label="Customer-data classifier model",
        group="ai",
        type="str",
        control="text",
        default="",
        sensitivity="needs_care",
        env_var="MOORING_AI_ROUTING",
        help="The model/deployment on that endpoint which judges every outbound turn "
        "and decides whether it needs the customer-data route. A small, cheap model is "
        "the usual choice. It sees the same text the coding model would.",
    ),
    SettingSpec(
        key="ai.routing.coding_model",
        accessor="ai_routing_local_coding_model",
        label="Customer-data coding model",
        group="ai",
        type="str",
        control="text",
        default="",
        sensitivity="needs_care",
        env_var="MOORING_AI_ROUTING",
        help="The default model/deployment that answers a customer-data conversation. "
        "It must also appear in the list below.",
    ),
    SettingSpec(
        key="ai.routing.coding_models",
        accessor="ai_routing_local_coding_models",
        label="Customer-data models offered",
        group="ai",
        type="list",
        control="tags",
        default=[],
        sensitivity="needs_care",
        env_var="MOORING_AI_ROUTING",
        help="Every model/deployment you are willing to send customer information to. "
        "Leave empty to offer only the coding model above; list two or more to get a "
        "picker in chat. The coding model above must be one of them.",
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
        key="ai.code_index",
        accessor="ai_code_index",
        label="Read the team code library (reusable helpers)",
        group="ai",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="weakens",
        env_var="MOORING_AI_CODE_INDEX",
        weaken_value=True,
        confirm="Turning the code library ON reads your team's importable .py helper "
        "modules and sends the copilot their API SKELETON — function/class names, "
        "signatures, type hints, and DOCSTRINGS (never a function body or any data "
        "value). The skeleton is value-free by construction, but docstrings are prose "
        "your team wrote, so this is a weaker tier than the value-blind schema. Modules "
        "are parsed, never imported or run. Continue?",
        help="Let the copilot discover and REUSE your team's helper functions/classes "
        "(from importable .py under the synced folders) instead of re-implementing them. "
        "Off by default; a per-module opt-out lives in the synced mooring.toml.",
    ),
    SettingSpec(
        key="ai.notebook_catalog",
        accessor="ai_notebook_catalog",
        label="Let the copilot search every notebook (repo-wide catalog)",
        group="ai",
        type="bool",
        control="toggle",
        default=False,
        sensitivity="weakens",
        env_var="MOORING_AI_NOTEBOOK_CATALOG",
        weaken_value=True,
        confirm="Turning the notebook catalog ON widens what the copilot sees from the "
        "ONE notebook you have open to EVERY notebook in the repo. For each it gets the "
        "`# H1` title, the imports, and the inputs/checks/SQL tables the source declares "
        "— never another notebook's code, its outputs, or a run receipt. Those facts are "
        "value-free by construction, but the title is prose your team wrote (scanned, "
        "like a docstring), so this is a weaker tier than the value-blind schema. "
        "Continue?",
        help="Let the copilot find work a teammate already did — 'does anyone already "
        "reconcile the GL feed?' — instead of rebuilding it. Off by default; a notebook "
        "you have turned AI off for is left out. The hub's own search box indexes the "
        "same facts locally either way.",
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
        key="ai.apply_guard",
        accessor="ai_apply_guard",
        label="Check a proposed cell before it is applied",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        # "needs_care", not "weakens": the page renders "weakens" as *Weakens privacy*,
        # and this guard is not a privacy control — nothing about what the model SEES
        # changes when it is off. The weakening it does is to write safety, which the
        # confirm below spells out; `weaken_value` (not `sensitivity`) is what makes
        # the endpoint demand that confirm.
        sensitivity="needs_care",
        env_var="MOORING_AI_APPLY_GUARD",
        weaken_value=False,
        confirm="Turning the Apply check OFF applies every cell the copilot proposes "
        "with no prompt — including one that deletes files, runs a program, or drops a "
        "table. Undo restores the notebook's text, so it takes back a cell that "
        "computes; it cannot take back a deleted file. This check is the only thing "
        "standing between a proposed cell and that kind of side effect. Continue?",
        help="Applied cells run the moment they land, so mooring reads each one first "
        "and asks before applying anything Undo cannot take back. Ordinary work — "
        "reading data, dataframes, plots, writing a new report file — applies silently.",
    ),
    SettingSpec(
        key="ai.apply_runs",
        accessor="ai_apply_runs",
        label="Run an applied cell straight away",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="needs_care",
        env_var="MOORING_AI_APPLY_RUNS",
        weaken_value=True,
        confirm="With this ON, a cell you Apply EXECUTES as soon as it lands in the "
        "notebook — you read its code and its result at the same moment. Turning it "
        "off stages the cell instead: it arrives marked stale and nothing runs until "
        "you press run. Continue?",
        help="Apply = add and run. Turn this off to make Apply = add only: the cell "
        "appears in your notebook marked stale and you run it yourself once you have "
        "read it. Slower, but no code the copilot wrote ever runs unasked.",
    ),
    SettingSpec(
        key="ai.auto_apply",
        accessor="ai_auto_apply",
        label="Let the copilot apply reversible changes itself",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        # "needs_care", not "weakens", for the same reason as ai.apply_guard: the page
        # renders "weakens" as *Weakens privacy*, and nothing the model SEES changes
        # here. What changes is who presses the button — spelled out in the confirm.
        sensitivity="needs_care",
        env_var="MOORING_AI_AUTO_APPLY",
        weaken_value=True,
        confirm="With this ON, a change the copilot writes lands in your notebook as "
        "soon as it is written — there is no Apply button in between — and mooring "
        "hands the model back what happened so it can correct itself. The check above "
        "still reads every cell first and still HOLDS anything Undo cannot take back, "
        "so this never lets an irreversible cell through unasked; what it removes is "
        "your look at the ordinary ones before they land. Continue?",
        help="Apply without the click, for changes Undo can take back: the copilot "
        "writes, sees the result, and fixes its own mistakes in one turn. Turn this "
        "off to go back to propose-then-Apply — nothing touches the notebook until you "
        "press the button. Either way, the check above still holds the irreversible "
        "cells for your confirm.",
    ),
    SettingSpec(
        key="ai.auto_run_report",
        accessor="ai_auto_run_report",
        label="Re-run the notebook to report a failure back",
        group="ai",
        type="bool",
        control="toggle",
        default=True,
        sensitivity="needs_care",
        env_var="MOORING_AI_AUTO_RUN_REPORT",
        weaken_value=True,
        confirm="With this ON, mooring may RE-RUN your notebook by itself when an "
        "applied cell did not complete, so the copilot gets the failure back without "
        "you relaying it. That executes your notebook's code again without you asking "
        "— which is fine for a read-and-compute notebook and is not free for one that "
        "writes somewhere. The run is the same value-free smoke path mooring already "
        "uses, and no value ever comes back to the model. Continue?",
        help="When a change the copilot made does not complete, let mooring re-run the "
        "value-free smoke check itself and hand the model the failure, instead of "
        "waiting for you to paste it. Turn it off and the model is still told what "
        "happened — mooring just will not re-run anything on your behalf.",
    ),
    SettingSpec(
        key="ai.max_tool_iters",
        accessor="ai_max_tool_iters",
        label="Tool-call ceiling per turn",
        group="ai",
        type="int",
        control="number",
        minimum=1,
        maximum=10000,
        default=200,
        env_var="MOORING_AI_MAX_TOOL_ITERS",
        help="A backstop against a runaway loop, not a work budget — set high on "
        "purpose so a long analysis runs to the end. Press Stop in the chat (or Esc) to "
        "end a turn you have seen enough of; lowering this only makes the copilot give "
        "up mid-thought.",
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
