---
icon: lucide/database
---

# Copilot reads the Power BI semantic model

!!! success "Status: phases 1–3 implemented"
    Phases 1–3 shipped 2026-07-02: the L2 allowlist extractor
    (`src/mooring/pbip_model.py`, covered by both `.importlinter` lean-core
    contracts), the `mooring ai model check` transparency CLI, the three
    copilot tools (`mooring_get_semantic_model` / `mooring_describe_model_table`
    / `mooring_get_measure`, gated like the dictionary trio and scrubbed at
    egress), the `semantic_models_text` fragment in
    `egress.build_system_context`, the `[ai] semantic_model` switch +
    `MOORING_AI_SEMANTIC_MODEL` + Settings-page entry, the synced per-model
    opt-out (`[ai] disabled_semantic_models`, hub action + `/api/ai/model/toggle`),
    and the artifact-row `model: {tables, measures}` summary (mtime-cached).
    The [ai-privacy](../../admins/ai-privacy.md) headline widened to "schema +
    authored expressions" in the same change.

    **Open:** phase 4 (the legacy `model.bim`/TMSL reader, render caps for
    100+-measure models, and the optional `mooring_search_semantic_model` tool)
    is deliberately deferred. Two documented behaviours to know: the per-model
    opt-out takes effect at the **next chat open** (tools are bound at session
    creation; there is no per-model session teardown), and **batch builder
    sessions do not get the model tools** (the batch planner consumes only the
    first two context elements by design).

## Problem

For many finance teams the single source of truth is not a parquet file — it is the
Power BI **semantic model**: the tables, columns, relationships, and measures whose
DAX encodes the business logic (`[Gross Margin %]`, fiscal calendars, currency
rules). Mooring already syncs PBIP projects file-by-file and groups them into one
hub artifact (`src/mooring/pbip.py` — `group()`, `aggregate_state()`, `launch()`),
but the copilot is completely blind to them. An analyst who asks "recreate the
Gross Margin % measure in polars" has to re-type the measure's DAX from memory into
the chat, every time, for every measure.

That is exactly the kind of context the copilot is built to consume. A PBIP's
model definition (TMDL text under `<name>.SemanticModel/definition/`, or the older
`model.bim` JSON) is **schema and authored code** — the same value-free class as
the notebook source the model already reads via `mooring_read_notebook_source`.
The gap is not a privacy barrier; it is a missing extractor and a missing tool.

The prize is the actual Power-BI-to-Python bridge: with the real DAX in context,
"recreate `[Gross Margin %]` over `data/sales.parquet`" becomes answerable
precisely — something a generic copilot can only do by reading the data too, which
mooring structurally refuses to do.

## Design

A new L2 module extracts an **allowlist-based skeleton** of a synced semantic
model:

- **In**: table names; column names + `dataType`; relationships
  (`fromTable.fromColumn` → `toTable.toColumn`, cardinality); measure names,
  DAX expressions, format strings, display folders; calculated-column DAX.
- **Out, never parsed into the result**: partition/source **M expressions**
  (they routinely embed server names, file paths, and credentials), **roles and
  RLS filter expressions** (security config; can embed usernames and literal
  entitlement values), annotations, `extendedProperties`, translations, and any
  construct the parser does not recognise. Allowlist means unknown → dropped.

Authored DAX *can* embed literal values (a hard-coded customer list in a measure
filter), so every string that leaves the workspace gets the same treatment as
notebook source: it routes through `egress.scrub_text()` in
`src/mooring/ai/egress.py`, and the opt-in PII scanner applies (the secrets
scanner in `src/mooring/ai/secrets.py` covers team-context files only).

User-visible behaviour:

- **Chat.** When the workspace contains a PBIP semantic model (and the feature is
  on), the copilot gains three new value-conscious tools, mirroring the data-
  dictionary trio in `src/mooring/ai/tools.py`: a model **summary** (tables +
  measure *names*, no DAX — cheap to read), a per-**table** description (columns +
  that table's measures with DAX), and a single-**measure** fetch. Selective
  retrieval, never a whole-model dump — large models stay out of the context
  window. The system context gains one names-only line ("this workspace has a
  Power BI semantic model: `Sales` — use the model tools"), so the model knows to
  look.
- **Hub.** The PBIP artifact row (rendered by `buildArtifactRows()` in
  `src/mooring/hub/static/app.js`) gains a model summary in its detail line —
  "12 tables, 48 measures" — and an actions-menu entry to turn the copilot's
  model access off for that project, written to the **synced** `mooring.toml`
  like the existing per-notebook AI opt-out.
- **CLI.** A new `mooring ai model check` prints exactly what the extractor would
  emit for each model — which files were read, which tables/measures were kept,
  what was excluded, and any scrubber findings — the same pre-flight transparency
  idiom as `mooring ai dictionary check` and `mooring ai pii check`
  (`cmd_ai_dictionary_check` / `cmd_ai_pii_check` in `src/mooring/cli.py`).

Gating: a new per-machine `[ai] semantic_model` switch (default **on** — the
content is the same class as notebook source, which is always sent) plus a new
synced per-model opt-out `[ai] disabled_semantic_models` in the workspace
`mooring.toml`, mirroring `[ai] disabled_notebooks` in
`src/mooring/workspace_config.py`. The opt-out travels with the repo, so a BI
owner can fence off one model for the whole team.

## Architecture fit

- **New L2 module `src/mooring/pbip_model.py`** — the TMDL/BIM extractor:
  `find_models(workspace, folders)`, `extract_model(path) -> SemanticModel`
  (new dataclasses: tables, columns, relationships, measures), and value-conscious
  renderers (`render_summary`, `render_table`, `render_measure`). Stdlib-only
  (text + `json`), so the frozen `.pyz`/`.exe` needs no new dependency.
  *Adaptation from the ideation sketch:* the sketch put the extractor in
  `pbip.py`, but that module is deliberately a small grouping/launch unit that
  imports `mooring.sync`; the extractor needs none of that, so it lands as a
  sibling. It must be added to the `sync-domain-is-core` contract's
  `source_modules` in `.importlinter` so the "never imports ai/editor/adapters"
  rule protects it too.
- **L3 `src/mooring/ai/tools.py`** — a new `MODEL_TOOL_NAMES` trio registered in
  `build_tools()` only when a model is present and allowed (the
  `DICT_TOOL_NAMES` pattern; `available_tools` in `src/mooring/ai/session.py` is
  derived from the tools actually built, so the allowlist stays in lock-step).
  `ai` importing `pbip_model` is a normal downward L3→L2 import — `tools.py`
  already imports `schema` and `marimo_rt` the same way. No marimo, no HTTP, so
  the `marimo-internals-isolated` contract is untouched.
- **L3 `src/mooring/ai/egress.py`** — `build_system_context()` grows an optional
  `semantic_models_text` parameter (the names-only hint), scrubbed like every
  other fragment. Extending the sanctioned channel is *supposed* to be a
  review-visible change to this file — that is the point of the choke point.
  (Note: `build_system_context` lives in `ai/egress.py`; `ai/chat.py` only
  re-exports it for back-compat.)
- **L1 `src/mooring/ai_config.py`** — a new `semantic_model: bool = True` field
  on `AiConfig`, parsed in `load_ai_config()` with a `MOORING_AI_SEMANTIC_MODEL`
  env override.
- **L1 `src/mooring/workspace_config.py`** — new
  `disabled_semantic_models()` / `set_semantic_model_disabled()` mirroring the
  `set_ai_disabled` idiom (strict read, sorted+deduped write, prune-empty,
  atomic replace, `_WRITE_LOCK`). Paths only, never a value.
- **L4 `src/mooring/hub/server.py`** — `_files_artifacts()` adds a `model`
  summary field to artifact rows (cached by mtime, the `_notebook_cache` idiom);
  `_build_chat_context()` assembles `semantic_models_text`; the provider's
  `open_chat()` in `src/mooring/ai/copilot.py` and `CopilotChatSession` in
  `src/mooring/ai/session.py` carry the parsed models to `build_tools()` the way
  `dictionary=` travels today. A new endpoint `/api/ai/model/toggle` mirrors
  `/api/ai/notebook/toggle` (`api_notebook_ai_toggle`).
- **L4 `src/mooring/cli.py`** — the new `mooring ai model check` subcommand.

All imports point down; no existing contract in `.importlinter` is loosened.

## Implementation plan

1. **Extractor + CLI transparency (M).** New `src/mooring/pbip_model.py`:
   an indentation-scoped line parser for the TMDL subset on the allowlist
   (`definition/model.tmdl`, `definition/relationships.tmdl`,
   `definition/tables/*.tmdl` — `partition ... = m` blocks skipped without
   capture; `definition/roles/` never opened), tolerant of unknown constructs;
   `find_models()` discovers `<name>.SemanticModel/` dirs under the synced
   folders (reusing `ARTIFACT_DIR_SUFFIXES` from `src/mooring/pbip.py`). Add
   `cmd_ai_model_check` to `src/mooring/cli.py` under the existing `ai`
   subparser, dispatched from `cmd_ai`. Ships alone as a useful lint.
2. **Copilot tools + gates + privacy docs (M).** Extend `build_tools()` in
   `src/mooring/ai/tools.py` with `mooring_get_semantic_model`,
   `mooring_describe_model_table`, `mooring_get_measure` — lookups by *name* in
   the pre-parsed in-memory model (like the dictionary tools, never a
   caller-supplied filesystem path), every result through `egress.scrub_text()`.
   Add the `AiConfig.semantic_model` knob and the
   `workspace_config.disabled_semantic_models` opt-out; wire
   `_build_chat_context()` → `open_chat()` → `CopilotChatSession` →
   `build_tools()`; add `semantic_models_text` to
   `egress.build_system_context()` and the tool-usage hint block in
   `src/mooring/ai/session.py`. Update `docs/admins/ai-privacy.md` (see Testing).
3. **Hub surface (S).** `_files_artifacts()` in `src/mooring/hub/server.py`
   emits `model: {tables, measures}` per artifact; extend the detail line in
   `buildArtifactRows()` and add a "Disable AI on model" entry via
   `actionsMenu()` in `src/mooring/hub/static/app.js`, calling the new
   `/api/ai/model/toggle`.
4. **Legacy `model.bim` + big-model ergonomics (S/M).** A TMSL-JSON reader
   mapping into the same `SemanticModel` dataclasses (plain `json`, low risk);
   render caps with an explicit "N more measures — ask for one by name" tail,
   and, if real models demand it, a `mooring_search_semantic_model` tool
   mirroring `mooring_search_dictionary`.

## Testing

All offline — this feature never touches GitHub, so not even the `responses`
mocks are needed; fixtures are TMDL trees written by the tests.

- **New `tests/test_pbip_model.py`** — the allowlist is the whole game, so pin it
  with `SECRET_VALUE_DO_NOT_LEAK`-style fixtures (the idiom from
  `tests/test_schema.py` / `tests/test_introspect.py`): plant the sentinel in a
  partition M connection string, an RLS filter expression, an annotation, and a
  translation, and assert it appears in **no** renderer output; plant a
  checksum-valid card number in a measure's DAX and assert the tool path drops
  that line via `egress.scrub_text`. Plus parser-tolerance tests (unknown TMDL
  constructs ignored, malformed file → fail-soft empty model).
- **Extend `tests/test_ai_tools.py`** — the model tools are absent from
  `available_tools` when no model exists, when `[ai] semantic_model` is off, and
  when the model is in the synced opt-out; results are value-free; name lookup
  cannot reach an arbitrary path.
- **Extend `tests/test_egress.py`** — `semantic_models_text` is scrubbed by
  `build_system_context` like every other fragment (keep the "assembler defined
  only in egress" contract test green).
- **Extend `tests/test_chat_context.py`**, **`tests/test_workspace_config.py`**
  (opt-out round-trip preserves other `mooring.toml` keys),
  **`tests/test_config.py`** (`MOORING_AI_SEMANTIC_MODEL` override), and
  **`tests/test_hub.py`** (artifact `model` field; `/api/ai/model/toggle`).
- **JS** (`node --test tests/js/`): unchanged unless the summary formatting is
  factored into a pure helper; the row rendering itself is screenshot-verified
  like other hub UI work.

## Risks and mitigations

- **Value-blindness rigour.** Calculated columns, measure filters, and RLS can
  embed literal values in authored expressions. Mitigations: the allowlist
  extractor (M partitions and roles are *never parsed*, not parsed-then-dropped),
  `egress.scrub_text` + the opt-in PII scanner on every expression that
  leaves, the pinned sentinel tests above, and the synced per-model opt-out.
  Residually this is the *notebook-source* class of guarantee, not the
  `schema.py` class — best-effort scanning over authored code, not physical
  impossibility.
- **The headline claim must stay honest.** `docs/admins/ai-privacy.md` currently
  says "schema + notebook source". This feature widens that to "schema +
  authored expressions (notebook source, and measure/calculated-column DAX)".
  The docs update is a hard deliverable of phase 2, not a follow-up — shipping
  the tool without it erodes the flagship guarantee.
- **TMDL format churn.** TMDL is still evolving under Microsoft's feet. The
  parser is TMDL-first, allowlist-scoped, and tolerant (unknown constructs are
  dropped, a parse failure yields an empty model and a visible "could not read"
  note, never a crash); fixtures come from real Power BI Desktop saves and get
  refreshed when Desktop updates. `mooring ai model check` doubles as the
  drift detector.
- **Large models blow the context window.** Never dumped: the summary carries
  names only, detail is per-table/per-measure on demand, and renders are capped
  (phase 4). This mirrors how the data dictionary already stays out of context
  via `locality` seeding plus pull tools.
- **A `.SemanticModel/` folder without a readable definition** (or a
  report-only PBIP). `find_models()` treats it as no model — the tools simply do
  not register, matching how `build_tools()` skips dictionary tools for an empty
  index.

## Dependencies and sequencing

Independent of the sync-safety track ([push guard](push-guard.md),
[staleness guard](staleness-guard.md), [local safety net](local-safety-net.md))
and can ship in any order relative to it. Like the
[handover explainer](handover-explainer.md) and the
[traceback fixer](traceback-fixer.md), it is **copilot-extra-only** — nothing
here runs without `mooring[copilot]` except the `mooring ai model check` lint
and the hub row summary, which work in every install. It shares the
egress-choke-point discipline the traceback fixer also leans on: both extend the
sanctioned channel and therefore both land with an
[ai-privacy](../../admins/ai-privacy.md) update and pinned egress tests. The new
config knobs belong in [configuration](../../admins/configuration.md); the layer
map lives in [architecture](../index.md).
