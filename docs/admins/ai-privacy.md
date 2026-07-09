---
icon: lucide/shield-check
---

# Why the copilot can't see your data

Mooring's AI copilot helps analysts write notebook code while being **structurally
unable to see the data itself**. This page is for analysts who want assurance and
for security reviewers who need to verify the claim. The short version:

> The assistant only ever receives a dataset's **schema** (column names + types)
> and **authored expressions** — the notebook's **source code** and, when the
> workspace holds a Power BI project, the semantic model's **measure /
> calculated-column DAX**. It has no tool that can read a data file, a cell
> output, or a variable value — and mooring never sends those anywhere.

This structural guarantee covers the **dataset and notebook**. An admin can
additionally opt in to **team context** (instructions + a data dictionary) — text
your team authors. That is a deliberately *weaker*, non-structural channel; it is
off by default and described in [Team context](#team-context-opt-in-not-a-structural-guarantee)
below. When it is on, the headline above holds for your data, but the model also
sees whatever your team wrote into those files.

New to mooring? The [5-minute quickstart](../users/quickstart.md) walks through
installing (`uvx mooring`), signing in, and sharing a notebook with your team, and
[What the copilot can do](../users/ai-copilot.md) shows the copilot at work. This
page is the *why it's safe* companion to both — schema-only: it sees your column
names and types and your authored code (the notebook, and a Power BI model's
DAX), but never the data itself.

> Running a frozen `.pyz`/`.exe` build? Use `python mooring.pyz <cmd>` (or
> `mooring.exe <cmd>`) in place of the `mooring <cmd>` examples below.

## What the assistant receives

| Sent to the model | Why it's safe |
|---|---|
| **Schema** — column names, dtypes, row count | Built by `schema.py`, which reads only a parquet footer or a csv/xlsx header. It never materialises a row, so no value is ever produced — proven by the `test_schema.py` "value never leaks" tests. |
| **Live dataframe schemas** — names + dtypes of dataframes loaded in your kernel | Built by `ai/introspect.py`, which runs a **fixed, value-free probe** in your kernel and reads back only names + dtypes. Covers data loaded from *outside* the workspace. Value-free by construction, not by physical impossibility — see [Live dataframe schemas](#live-dataframe-schemas-data-outside-the-workspace). |
| **Notebook `.py` source** | A marimo notebook is pure Python; the data is loaded at *runtime* (`pl.read_parquet(...)`). The source is code, not data. |
| **Power BI semantic model** — table/column names + types, relationships, measure and calculated-column DAX | Extracted by an **allowlist** parser (`pbip_model.py`) from a synced PBIP's TMDL text: partition/source **M expressions** are skipped *without being captured*, **RLS roles** and **translations** are never opened, annotations and unknown constructs are dropped. DAX is authored code — the same class as notebook source, with the same best-effort scanning caveat. See [the semantic model](#power-bi-semantic-model) below. |
| **Your chat messages** | What you type. The `/explain` walkthrough (and its "Add as notes cell" follow-up) sends **fixed, value-free prompt text** over this same channel — no new egress surface. A pasted **traceback** is rewritten value-safe and held for your confirmation before it can leave — see [Pasted tracebacks](#pasted-tracebacks). |

## What it never receives

- **Cell outputs / dataframe previews** — these are where real values appear.
- **Variable *values*.** Mooring may read a live dataframe's **schema** (names +
  dtypes — see [Live dataframe schemas](#live-dataframe-schemas-data-outside-the-workspace)),
  but never a stored value or other kernel state.
- **Raw error tracebacks.** A traceback can embed values (`KeyError: 'ACME Ltd'`),
  and mooring never captures one itself — but an analyst can *paste* one into the
  chat. That paste is structurally rewritten value-safe and held for an explicit
  confirm before anything leaves; the raw paste is never stored, so no code path
  can forward it. What survives the rewrite is best-effort, not structural — see
  [Pasted tracebacks](#pasted-tracebacks) for the exact contract.
- **The contents of any data file.**

## The four structural guarantees

1. **Single choke point for the system context.** The context handed to the model
   is assembled in one place (`ai/chat.py:build_system_context`) — from the schema
   text and the notebook source, plus (only when team context is enabled) the team
   instructions and the value-minimised data-dictionary slice. Two further egresses
   exist by design and are value-free by construction: your **chat turns**, and the
   agent's **tool reads** (it can re-read the notebook source via
   `mooring_read_notebook_source`, and fetch dataset schemas). The opt-in
   [structured-PII scan](#structured-pii-pre-flight-scan-opt-in-best-effort) runs at
   all of these, not only `build_system_context`.
2. **Value-free tools only.** The agent is given mooring's own tools (`ai/tools.py`):
   list datasets, get a schema, read the notebook source, and *propose* a cell —
   each value-free by construction. When a data dictionary is configured, three more
   tools (`list_tables`, `describe_table`, `search_dictionary`) serve it; they look
   up tables by name in an **in-memory parsed index** (never a filesystem path) and
   return only the five allowlisted fields (see [Team context](#team-context-opt-in-not-a-structural-guarantee)).
   When the workspace holds a Power BI semantic model, three more
   (`get_semantic_model`, `describe_model_table`, `get_measure`) serve its
   pre-parsed allowlist skeleton the same way — name lookups in memory, every
   result through the egress scrub (see [the semantic model](#power-bi-semantic-model)).
   The session's `available_tools` allowlist contains **only** these tool names, so
   the SDK's built-in file-reading and shell tools are **not available**; a
   **deny-all permission handler** rejects anything else as a backstop; and the
   agent runs with an **empty working directory** so there are no data files within
   its reach.
3. **Applying a cell only writes source; mooring never opens a marimo websocket.**
   When you Apply a proposed cell, mooring writes the cell's **source code** into
   the notebook's `.py` file (via marimo's own codegen); the editor, launched with
   `--watch`, reloads and runs it. mooring never reads cell outputs, and never
   connects a marimo *websocket* — and outputs, dataframe previews, and variable
   values are delivered *only* over that websocket. So a value cannot travel back
   through mooring to the model. (The cell runs in *your* kernel; only your browser
   sees the result.) Live-schema introspection ([below](#live-dataframe-schemas-data-outside-the-workspace))
   keeps this invariant: it pushes a fixed probe in over HTTP and reads back only a
   names-and-dtypes file that probe wrote — never a cell output, never the websocket.
4. **marimo's own AI is turned off.** marimo ships a built-in AI assistant that
   *does* send sample values to whatever model it's configured with. Mooring
   disables it in every editor it launches by writing a `.marimo.toml`
   (`ai.enabled = false`, `completion.copilot = false`) into the workspace, which
   marimo reads ahead of any personal config.

Nothing about a conversation is persisted: the session store, telemetry, config
discovery, skills, file hooks, and host-git access are all switched off.

## Choosing the AI backend: Copilot or an OpenAI-compatible endpoint { #ai-backend }

The copilot ships two interchangeable backends. Pick one **per machine** in the hub
**Settings ▸ AI copilot ▸ AI backend** dropdown, or in `config.toml`:

```toml
[ai]
provider = "copilot"   # the default (GitHub Copilot SDK) — or "openai"
model = ""             # optional: pin a model id (the hub's picker lists what's available)
```

The `"openai"` backend is a generic **OpenAI-compatible** client: with a base URL it
talks to OpenAI, Azure OpenAI, an enterprise gateway (LiteLLM), an aggregator
(OpenRouter / Together / Groq), or a **local server** (vLLM / Ollama / LM Studio).
Switching backend changes *where* the value-free schema + source are sent — it stays
value-blind either way, but the destination changes, so it is a deliberate
(needs-care) choice.

The value-blindness guarantees above are **provider-independent** — they are
enforced *before* egress, in mooring's own code (`build_system_context`, the
value-free tool handlers, the PII/traceback guards). Only the dataset **schema**,
the notebook **source**, and a Power BI model's authored **DAX** ever leave, for
either backend. Switching provider changes *where* that already-value-free text is
sent, never *what* leaves.

The OpenAI backend (the `mooring[openai]` extra) is built on the **Chat Completions
API**, chosen precisely because value-blindness there is *structural*, not a
convention:

- **No hosted tools, ever.** Chat Completions' `tools=` accepts function specs
  only — there is no `web_search`, `file_search`, or `code_interpreter` the API
  will honour — so the model *cannot* reach the web, a file, or a code sandbox.
  mooring registers **only its own value-free function tools**; a self-driven tool
  loop dispatches them by name and refuses any unrecognised name. (This replaces
  Copilot's allowlist + deny-all-permission + empty-working-dir hardening, which
  guard against a *built-in agent's* file/shell tools that OpenAI simply doesn't
  have.)
- **Same single context choke point**, the same value-free tools (re-expressed as
  function specs from the identical handlers), the same source-only Apply, and
  marimo's own AI stays off.
- **No server-side retention.** Every request is sent with `store = false`, so the
  schema/source are not retained by OpenAI's stored-completions feature; the
  conversation lives only in memory for the session's life.
- The OpenAI **API** (unlike the ChatGPT consumer product) does not train on API
  inputs or outputs by default, and Zero-Data-Retention is available for eligible
  enterprise accounts.

### The API key stays on your machine (and is optional for local endpoints)

The API key is a secret and is **never** written to the synced `mooring.toml` (that
would hand it to the whole team on push). It is resolved locally, in order:

1. `MOORING_OPENAI_API_KEY` (env — beats everything, mirrors `MOORING_TOKEN`);
2. the OS credential store, set with **`mooring ai key set`** (reads the key from a
   no-echo prompt; `mooring ai key clear` removes it);
3. `OPENAI_API_KEY` (the SDK's own env var, for convenience).

In the hub, the AI card's **Set API key** button stores the key the same way (the OS
keyring) and validates it. A **local / self-hosted endpoint usually needs no key** —
set the base URL and leave the key empty, and the backend connects without one.

### Pointing at your own endpoint

`openai_base_url` and `openai_api_version` (both value-free — a URL and a version,
never a secret, so they are safe in config and editable in Settings) select the
endpoint:

```toml
# OpenAI itself — leave the base URL empty
[ai]
provider = "openai"
model = "gpt-4o"

# A local model server (no key needed)
[ai]
provider = "openai"
openai_base_url = "http://localhost:11434/v1"   # Ollama; vLLM / LM Studio are similar
model = "llama-3.1-70b"

# An aggregator / gateway
[ai]
provider = "openai"
openai_base_url = "https://openrouter.ai/api/v1"
model = "meta-llama/llama-3.1-70b-instruct"

# Azure OpenAI — keeps traffic in your own tenant/region
[ai]
provider = "openai"
model = "my-gpt-4o-deployment"                  # on Azure, the DEPLOYMENT name
openai_base_url = "https://my-res.openai.azure.com"
openai_api_version = "2024-10-21"               # set → the AzureOpenAI client
```

Value-blindness is unchanged for every one of these — only the value-free schema,
source, and DAX ever leave, to whichever endpoint you configure.

## Turning the copilot off for a notebook

Beyond the global `[ai] enabled` switch, the copilot can be turned off for an
**individual notebook** — the off switch for "this notebook now handles PII; don't
let AI touch it by mistake." A user flips it from the hub row (**Disable AI**) or
from the chat window's top bar; both call one endpoint that writes the notebook's
workspace-relative path into a **synced** `mooring.toml` at the workspace root
(`[ai] disabled_notebooks`).

Two properties make this a real control rather than a hidden button:

- **Enforced on every egress, not just the open.** Disablement is re-checked when a
  chat is opened, on every message **send**, and on every **apply** (apply writes the
  notebook, so it is the highest-value gate). A chat window opened before the toggle
  — or disabled from the hub while it is open — is refused and torn down on its next
  call. The check is keyed by the session's bound notebook, so a stale tab cannot slip
  a turn through.
- **It travels with the notebook.** `mooring.toml` rides pull/push/propose like any
  tracked file, so once pushed, everyone who syncs the repo gets the copilot turned
  off for that notebook too. It stores only notebook **paths** — never a value, so it
  is value-free by construction like everything else that leaves the workspace. (It is
  a single shared file: concurrent edits resolve through the normal conflict flow. A
  malformed `mooring.toml` is ignored when *reading* the opt-out — it fails *open*,
  re-enabling AI rather than wedging the hub — but *editing* it is refused so a bad
  file is never silently overwritten; the apply-time gate remains the backstop.)

## The Power BI semantic model: schema + authored DAX { #power-bi-semantic-model }

When a synced PBIP project's `<name>.SemanticModel/` folder is in the workspace,
the copilot can read the model's **skeleton** — so "recreate `[Gross Margin %]`
in polars" is answered from the measure's *real* DAX instead of a guess. It is
**on by default** (`[ai] semantic_model = true`) because the content is the same
class as the notebook source the assistant always sees: authored code, never
data. What keeps it that way:

- **An allowlist extractor** (`pbip_model.py`) parses the TMDL text and keeps
  only: table names, column names + `dataType`s, relationships, and measure /
  calculated-column DAX with format strings and display folders. The blocklist
  is not the mechanism — *everything not on that list is dropped*, and the three
  places values actually live in a model definition never enter the parse at
  all: **partition/source M expressions** (connection strings, server names,
  credentials) are skipped without their bodies being captured; **RLS role
  files** (filter expressions can embed usernames and entitlement values) and
  **translations** are never opened; annotations and unknown constructs are
  dropped. A parse failure yields an empty model, never a crash.
- **Selective retrieval, never a dump.** The system context gets one names-only
  line ("this workspace has a semantic model: `Sales` — 12 tables, 48
  measures"); the DAX itself is only fetched through the three per-name tools,
  so a large model stays out of the context window.
- **Every rendered string passes the egress scrub.** Authored DAX *can* embed a
  literal value (a hard-coded customer list in a measure filter), so each tool
  result and the context hint route through `egress.scrub_text` — the same
  checksum-PII floor as notebook source — and the opt-in
  [PII scan](#structured-pii-pre-flight-scan-opt-in-best-effort) applies.

**The honest classification:** this is the *notebook-source* class of guarantee
— best-effort scanning over code a human wrote — not the `schema.py` class of
physical impossibility. A value typed into a DAX expression is visible to the
assistant exactly as a value typed into a notebook cell is. The pinned tests
(`tests/test_pbip_model.py`, `tests/test_ai_model_tools.py`) plant a sentinel
value in a partition M connection string, an RLS role filter, an annotation, and
a translation, and prove it appears in **no** output.

Two off switches, mirroring the notebook controls:

- `[ai] semantic_model = false` (or `MOORING_AI_SEMANTIC_MODEL=0`, or the
  Settings page) turns the feature off per machine.
- The **synced** per-model opt-out — `[ai] disabled_semantic_models` in the
  workspace `mooring.toml`, written by the hub row's "Disable AI on model"
  action — fences one model off for the whole team, like the per-notebook
  opt-out. It stores artifact **keys** (paths), never a value. Note the
  **next-open semantics**: tools are bound when a chat opens, so a chat window
  already open keeps its model tools until it is closed; new chats respect the
  toggle immediately.

Run **`mooring ai model check`** to see exactly what the extractor would emit —
per model: which files were read, which tables/measures were kept, what was
excluded (partitions skipped, roles/translations never opened, constructs
dropped), and any scrubber findings — *offline*, before the copilot ever sees it.

## Live dataframe schemas (data outside the workspace)

`schema.py` can only inspect data files that sit *inside* the workspace. But real
data often lives **outside** it — a network share, a warehouse export, a database
connection, a path built at runtime — and the schema most useful for writing code
is frequently a *derived* frame (a join/filter result) that no file holds. To help
there, mooring can read the schema of the dataframes **already loaded in your
running kernel**. It is **on by default**, refreshed on every chat turn (so a frame
you load after opening the chat is picked up without reopening), and value-free — but,
like team context, its safety comes from *how it is built*, not from physical
impossibility, so it is documented here in full. Turn it off with
`[ai] live_schema = false`.

How it stays value-blind (`ai/introspect.py`):

- **The code is fixed, never model-authored.** Mooring pushes one frozen probe into
  the kernel via `POST /api/kernel/run`. The probe walks the kernel namespace, and
  for each polars/pandas dataframe emits **only** `{name, columns: [(name, dtype)],
  n_rows}` using schema-only accessors (`collect_schema()` / `.schema` / `.dtypes`
  — never `.head`, `.row`, or `.collect` of data). The one dtype that embeds
  author-defined strings, polars `Enum`, is reduced to the bare type name.
- **No new value channel.** `/api/kernel/run`'s HTTP response carries no outputs
  (verified: `scripts/spike_marimo_http_control.py`), and mooring still never opens
  the marimo websocket. The probe hands its result back through a **sidecar file it
  writes**, which mooring reads once and deletes.
- **Fail-closed on the way back.** The reader (`_parse_frames`) accepts only the
  `{name, columns: [[str, str]], n_rows: int}` shape and drops everything else, so a
  value can't ride back on a key mooring doesn't read.
- **The per-turn refresh adds no new value channel.** The schema is captured at
  chat-open *and* re-probed on each turn through the **same** frozen probe and
  fail-closed reader; a turn re-states the schema only when the kernel changed, and an
  unchanged kernel is not re-sent. The refresh re-states already-value-free schema —
  it opens no path a value could take that the open-time capture did not.

Honest caveat: unlike `schema.py` (which physically only ever reads a file header),
this probe runs in a namespace that *contains* values. Its value-blindness is the
correctness of that frozen probe plus the fail-closed reader — pinned by the
`SECRET_VALUE_DO_NOT_LEAK` tests in `tests/test_introspect.py`, which load frames
full of secret values (including an `Enum` whose categories are secret) and prove
none reach the readback. If introspection can't run (no live session, frames not yet
loaded), mooring silently falls back to the file-based schema.

## Team context (opt-in): not a structural guarantee

The four guarantees above are *structural* — they hold no matter what. **Team
context is different and weaker, by design**, so it is **off by default**
(`[ai] context = false`). When an admin turns it on, mooring reads the workspace's
`context/` folder and feeds the model:

- **`context/instructions.md`** — free-text guidance, sent **verbatim**. This is
  the residual leak vector: a human can type anything, so whatever is written here
  reaches the model. It is the `copilot-instructions.md` equivalent.
- **`context/dictionaries/*.yaml`** — per-domain data dictionaries (dbt
  `schema.yml` and other shapes auto-detected). mooring parses each file and keeps
  **only five fields** per column — `name`, `type`, `nullable`, `relationship`,
  `description` — dropping everything else (sample values, defaults, enums, test
  literals, `meta`/`comment` blobs). It then serves only the slice relevant to your
  current notebook/dataset, with the rest reachable via the dictionary tools.

Two honest caveats:

- **The dictionary is *minimised*, not *structurally* value-free.** Unlike
  `schema.py` (which never materialises a value), the dictionary's `description` is
  free text a human wrote; if someone types a real value into a description, it can
  reach the model. The five-slot allowlist (`ai/datadictionary.py`) and a
  best-effort **secret scan** (`ai/secrets.py`, which withholds an instructions file
  and drops a description on a high-confidence hit) reduce the risk — but the
  primary controls are the allowlist and **human review**, not the scanner. Regex
  scanning cannot catch a customer name, an internal account code, or a value typed
  into prose.
- **`context/` is shared.** If your team syncs `context/` via GitHub, these files
  go to the whole team. Treat them like code: review changes, and never paste real
  values or secrets.

Run `mooring ai dictionary check` to see exactly how your files parse — which shape
was detected, how many tables/columns were kept, which keys were dropped, and any
secret-scan findings — *before* enabling the feature or sharing the files.

### Multiple context folders: team offer + per-user subscription

A repo can offer **more than one** context folder, and the choice is **per-repo**.
Two planes, deliberately separated:

- **The team OFFER (synced, team-wide).** A curator lists the offered folders in the
  synced `mooring.toml` `[ai] context_folders` — via the hub's per-folder **"AI
  context"** toggle or `mooring ai context add/remove`. This is *AI governance*, the
  same trust model as `disabled_notebooks`/`featured_folders`: **anyone in repo mode
  can widen the team's model-readable ceiling by pushing `mooring.toml`**, so review
  it like code.
- **The per-user SUBSCRIPTION (per-machine).** Each teammate can narrow which offered
  folders *their* copilot reads — the hub checklist or `mooring ai context use/unuse`,
  stored in their own `config.toml` `[repos.<alias>].ai_context_folders`. Unset = read
  the whole offer; an explicit empty selection reads nothing.

The load-bearing invariant: **a subscription can only ever narrow, never widen.** The
read set is always `subscription ∩ offer`, and the **whole offer** rides sync for any
consented teammate — so every folder the model can read is a folder that went through
the **pre-push secret/PII scan**. A user's personal pick is provably a subset of that
scanned, synced set.

Each folder is read independently: the per-file **secret withhold** and the per-file
size cap run **per folder**, so a poisoned folder can neither blank a clean sibling nor
escape its own withhold; the combined instructions are also aggregate-size-capped. When
two folders define the same dictionary table (the domain is the file stem, not
folder-unique), the first (sorted-folder) wins and the shadowed copy is **surfaced as a
`mooring ai dictionary check` finding** — never silently dropped.

One honest deviation to note: because the whole offer syncs for any consented teammate,
an offered folder rides `pull` to a teammate who has **not** subscribed to it — harmless
value-free files on disk that never enter the model's context. (`context` off is still
"neither read nor synced".)

## Structured-PII pre-flight scan (opt-in, best-effort)

The guarantees above stop the *data* from reaching the model. They cannot stop a
human from **typing a real value** into a cell or the chat —
`df.filter(pl.col("pan") == "4012 8888 8888 1881")`, or "why does account
4012888888881881 fail?". As a thin extra floor, mooring can scan text on its way
out for **well-formed structured identifiers** and warn before it leaves. It is
**off by default** (`[ai.pii] enabled = false`) and, like team context, its safety
is best-effort, not structural.

**What it catches** (precision over recall): checksum-validated **payment cards**
(Luhn), **IBANs** (mod-97), and **NHS numbers** (mod-11), plus shape-anchored
**emails** and **UK NINOs**. **What it does not catch, by design:** addresses,
account narratives, **UK sort codes**, **bank account numbers**, US SSNs, phone
numbers, dates of birth, IP addresses, or any value split across two messages.
Person **names** are out of reach of the structured scan too, but can be caught by
the optional local-NER pass below. **A clean scan is not a value-free guarantee** —
it is a safety net for the obvious, well-formed cases, and it complements (never
replaces) the structural value-blindness above.

It runs at every egress, and every finding is value-free — a line number and a
*kind* (`payment card`, `email address`, …), never the matched value:

- **Your chat prompt.** With `block_prompt = true` (the default once the feature is
  on), a prompt that looks like it contains a card/IBAN/NHS/email/NINO is **held**;
  you see which kinds tripped it and must click **"Send anyway"** — nothing reaches
  the model until you confirm. (Set `block_prompt = false` for a warn-only advisory.)
- **The notebook source and its schema.** On opening the copilot you get a one-time,
  value-free banner if the notebook or a dataset schema looks like it contains PII.
  The source is never rewritten (that would break your code). But a schema **column
  name** that is itself a value — the result of a pivot/transpose on a PII key, e.g.
  `df.pivot(on="customer_pan")` — is **withheld** from the schema the model sees.
- **Team context.** An `instructions.md` carrying a checksum-validated card/IBAN/NHS
  (or a secret) is withheld entirely; a stray email/NINO drops just that line; a
  data-dictionary description that trips the scan is dropped.

Run **`mooring ai pii check`** to scan your `context/` files and notebook sources
**offline** (no Copilot, no network) before enabling the feature — it prints
`path:line  kind` for each finding and never echoes a value. Put `# mooring: pii-ok`
on a line to retire a reviewed false positive.

Configure it under `[ai.pii]`: `enabled` (master switch), `block_prompt`
(hold-and-confirm vs. a warn-only advisory on the chat prompt), and
`scan_notebook_source` (the source/schema banner).

### The same scanners also watch the push channel

Since v0.5 the **push guard** points these detectors (plus the secret scanner)
at a second, always-on channel: files about to be **pushed to the team repo**.
A flagged file is withheld with a value-free `path:line kind` finding and an
explicit confirm ("Push anyway"), which the synced `mooring.toml` can escalate
to a hard block (`[guard] push = "block"`). This changes **nothing** about the
AI channel — same detectors, second consumer — and like them it is best-effort
defence in depth, not a guarantee: a clean push scan does not mean a file is
value-free. See the roadmap page
[push guard](../developers/roadmap/push-guard.md) for the design.

## Pasted tracebacks: sanitised and held (on by default, best-effort) { #pasted-tracebacks }

When a cell errors, the single most tempting act is to paste the traceback into
the chat — and tracebacks routinely embed data values: `KeyError: 'ACME Ltd'`,
`could not convert string to float: '£1,234'`, a repr of the offending row
inside a library frame. Mooring never captures a traceback itself (it reads no
cell outputs and never opens the marimo websocket), so a paste is the only way
one can reach the model — and that paste no longer travels raw.

The **traceback guard** (`[ai] traceback_guard`, **on by default**) detects a
traceback block in an outbound message and rewrites it **fail-closed** before
any egress, then **holds the turn**. What survives the rewrite:

- The **exception type** — `polars.exceptions.ColumnNotFoundError` is a code
  identifier, not data. The fixed chained-exception separator lines are kept too.
- **Frames that resolve into your workspace**: workspace-relative path, line
  number, and function — with the quoted source line **re-read from the local
  `.py` file**, never trusted from the paste. The re-read is restricted to paths
  that resolve **under the workspace** and end in `.py`, so a crafted frame can
  never make the sanitiser read a data file (pinned by `tests/test_traceback.py`).
- **Frames outside the workspace** (site-packages, stdlib) keep only a
  code-shaped file basename, the line number, and the function name; their
  source lines are dropped.
- The **exception message**, only when it is provably value-free: it matches a
  fixed allowlist of interpreter messages ("division by zero", …), or every
  quoted token in it already appears in text the model has been shown this
  session (the dataset schema, the live-kernel schemas, the notebook source).
  So `KeyError: 'revenue'` survives when `revenue` is a schema column — restating
  it reveals nothing new — while `KeyError: 'ACME Ltd'` becomes
  `KeyError: <redacted: 10 chars>`.

Everything else inside the detected block — an unrecognised line, a pasted
"source" line, a message that cannot be proven value-free — is redacted to a
shape-preserving placeholder. Parser gaps fail **closed**, never open.

The held turn shows a preview of *exactly* what will be sent, with one **Send
sanitised** button. Unlike the PII guard there is deliberately **no "send raw
anyway" escape**: only the sanitised rewrite is ever stored server-side, so no
code path can transmit the raw paste. Prose around the traceback is untouched —
it still goes through the [structured-PII prompt scan](#structured-pii-pre-flight-scan-opt-in-best-effort),
whose value-free findings ride the same hold card.

Honest caveats, in the same spirit as the scanners on this page:

- **Best-effort, not structural.** An analyst can still **retype a redacted
  value in prose** — the guard narrows the paste channel; it cannot close the
  keyboard. Frame basenames and function names are kept only when they look like
  code identifiers, but an identifier-shaped value would survive as one.
- **The off switch is a policy decision.** `[ai] traceback_guard = false` (or
  `MOORING_AI_TRACEBACK_GUARD`) turns the guard off per machine; flipping it off
  on the Settings page requires an explicit weakening confirm, and raw
  tracebacks then reach the model unchecked (aside from the opt-in PII scan).

Run **`mooring ai traceback check [FILE]`** (or pipe a traceback on stdin) to
see the exact rewrite **offline** — no Copilot, no network — before trusting the
guard. The offline preview has no chat session, so it redacts *more* than the
chat would (no known-token rescue), never less.

## Name detection (opt-in, local NER)

A person's name — `where name == "Jane Smith"` — has no checksum or fixed shape, so
the structured scan above cannot see it. The optional **name pass** (`ai/ner.py`)
closes that gap with a **local** zero-shot NER model ([GLiNER](https://github.com/urchade/GLiNER)),
shipped as the `mooring[pii]` extra so the lean install and the frozen `.pyz` stay
free of the heavy ML stack (torch + transformers). It is **off by default**, only
acts when `[ai.pii] enabled` is also true, and is **best-effort** (NER both misses
and false-positives — a clean scan is not proof of no names).

Its privacy properties match the structured scan:

- **Local only.** The model runs on the analyst's machine; the text is never sent
  anywhere to be scanned. The single network touch is a **one-time model download**
  from Hugging Face on first use — pre-fetch it on a managed/offline network with
  **`mooring ai pii model`**.
- **Value-free findings.** GLiNER returns the matched name; mooring reads **only**
  the label and character offset, maps it to a line number, and **drops the text**.
  A finding is `(line, "person name")` — never the name — so it logs and streams
  over SSE as safely as the structured kinds. Pinned by `tests/test_ner.py`.
- **No pickle, pinned.** The default model (`gliner-community/gliner_small-v2.5`) is
  loaded as its **safetensors** `bf16` variant — `mooring ai pii model` fetches *only*
  the safetensors file, never the repo's `pytorch_model.bin`, so nothing is unpickled.
  It is **pinned to a specific commit** (`name_model_revision`) for reproducibility and
  so a security review is against a fixed artifact.
- **Same egress + UI.** A flagged chat prompt is held with the same "Send anyway"
  confirm; `mooring ai pii check` runs the name pass too (when the model is already
  cached) for the offline lint. At the chat prompt, a configured-but-uninstalled extra
  **fails loud** (a `scan_error` advisory) rather than silently doing nothing; while the
  model is still downloading the name pass is skipped (the message is still structurally
  scanned) and the chat shows a "preparing model" status.

Configure under `[ai.pii]`: `detect_names` (on/off), `name_model` / `name_model_revision`
/ `name_model_variant` (which model, pinned commit, and safetensors variant —
`name_model_variant = ""` loads a repo's default weights file for a model that has no
variant safetensors), `name_labels` (entity labels to flag), and `name_threshold`
(confidence cut-off; raise for fewer, safer hits). GLiNER is zero-shot, so `name_labels`
is not limited to people — add `"organization"` to also flag **business names** (surfaced
as an `organization` finding); other entity types (e.g. `"address"`) work the same way.
Capitalised non-person terms make organisation detection more false-positive-prone, so it
stays out of the default. Install and enable:

```toml
[ai.pii]
enabled = true
detect_names = true
```
```
pip install "mooring[pii]"    # or uv add / uv tool install / uvx — quote the brackets
mooring ai pii model          # pre-download the model (recommended)
```

## Deploying name detection in an institutional / offline environment

The model download is the only part of mooring that reaches a non-GitHub host
(Hugging Face). In a locked-down environment, plan for:

- **Firewall allow-list.** Outbound HTTPS is needed to `huggingface.co` **and** the
  file backends — the LFS CDN and the newer **Xet** hosts (`cas-bridge.xethub.hf.co`,
  `*.xethub.hf.co`). Allow-listing only `huggingface.co` passes the metadata fetch and
  then fails on the actual download.
- **TLS / SSL-intercepting proxy.** mooring enables the **OS trust store** globally
  (`truststore`), so Hugging Face traffic honours your proxy's root CA automatically,
  the same way GitHub traffic does — no separate CA bundle needed in the normal case.
  `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` still take precedence if you set them.
- **Proxy / rate limits.** `HTTPS_PROXY` / `NO_PROXY` are honoured; set an `HF_TOKEN`
  to lift the anonymous-download rate limit (faster, fewer throttles).
- **Air-gapped (no egress).** Either point at an internal mirror with
  `HF_ENDPOINT=https://<your-hf-proxy>` (e.g. Artifactory/Nexus), **or** provision the
  cache out-of-band: run `mooring ai pii model` on a connected machine, copy
  `~/.cache/huggingface` (or set a shared `HF_HOME`) to the target machines, and set
  `HF_HUB_OFFLINE=1`. Relocate the cache with `HF_HOME` if the user profile is small
  or roaming.
- **Model governance.** The weights are a third-party artifact. The pinned default is
  safetensors (no code-execution-on-load risk that a pickle `pytorch_model.bin` carries),
  loaded locally; review the pinned `name_model` + `name_model_revision` through your
  model-risk process, and re-pin a new revision only after review.

### PyPI-only / fully air-gapped: the spaCy backend { #spacy-backend }

If Hugging Face is **unreachable at all** — no allow-list, no mirror, and your only
package channel is an internal PyPI — use the **spaCy** name backend instead of GLiNER.
spaCy's own models aren't on PyPI either (they ship from GitHub), so mooring republishes
an **MIT-licensed** model to PyPI as the `mooring-spacy-en-md` companion, pulled by the
`pii-spacy` extra. Nothing reaches Hugging Face or GitHub at install time.

You don't have to hand-pick the backend: `name_backend` ships as `"auto"`, which uses
the offline spaCy backend automatically whenever the `pii-spacy` extra and its model are
present (otherwise GLiNER). So **installing the extra is enough** — the only settings you
still choose are turning the guard and name detection on:

```toml
[ai.pii]
enabled = true
detect_names = true
name_labels = ["person", "organization"]
# name_backend = "auto"   # the default; auto-selects spaCy once pii-spacy is installed.
#                         # Pin it to "spacy" only if you want to force the offline backend
#                         # even when GLiNER is also installed.
```
```
pip install "mooring[pii-spacy]"   # spaCy + bundled model, both from PyPI (or uv add / uvx)
mooring ai pii doctor              # shows which backend will run + whether it's ready
mooring ai pii model               # verifies the model loads (nothing to download)
```

- **Delivery options if even the companion isn't on your mirror.** The model is a static
  folder, so deliver it however mooring itself reaches the box: have IT add the one static
  companion wheel to your internal PyPI mirror (the same channel that already serves
  `mooring`), or sideload the folder and point `[ai.pii] name_model` at its path — or, as
  the advanced fallback for a machine with no Python tooling at all, **bundle it into the
  frozen `.pyz`/`.exe`** your admin builds. The maintainer vendors the model once with
  `scripts/vendor_spacy_model.py`.
- **Same privacy posture.** Local-only, value-free `(line, kind)` findings — identical to
  GLiNER. The trade-offs are accuracy (spaCy `md` is solid for people/orgs but weaker than
  GLiNER) and **no confidence threshold** (`name_threshold` is ignored for spaCy; it relies
  on the label set). Org detection needs only the `"organization"` label above.

## The one thing to watch

Anything **you type into a cell or the chat** is, by definition, visible to the
assistant. If you hard-code a real value into a cell —
`df.filter(pl.col("ssn") == "123-45-6789")` — that literal is part of the source
the assistant can read. The chat reminds you of this; **never paste real values**.
The opt-in [structured-PII scan](#structured-pii-pre-flight-scan-opt-in-best-effort)
above catches *well-formed* cards/IBANs/NHS numbers/emails/NINOs as a safety net,
but it cannot catch a name, a sort code, an account number, or a value typed into
prose — so the rule stands regardless.

## Verifying it yourself

- **Read two files.** `ai/tools.py` is the only thing that builds tool results;
  `ai/cellwrite.py` is the only thing that writes a cell into the notebook (value-
  free source via marimo codegen — no kernel/output access, no websocket).
- **Run the tests.** `uv run pytest tests/test_schema.py tests/test_ai_tools.py
  tests/test_chat_session.py tests/test_notebook_control.py` — these assert that
  a fixture value (`SECRET_VALUE_DO_NOT_LEAK`) never appears in anything sent to
  the model, that the session is built with the value-blind options, and that the
  marimo channel is HTTP-only. For the team-context surface, `tests/test_datadictionary.py`,
  `tests/test_ai_dict_tools.py`, and `tests/test_context.py` assert that
  value-bearing keys are dropped, that the dictionary tools can't reach a file, and
  that a secret in an instructions/description field is withheld. For the Power BI
  semantic model, `tests/test_pbip_model.py` and `tests/test_ai_model_tools.py`
  prove a sentinel planted in a partition M expression, an RLS role, an annotation,
  or a translation reaches no output, and that the model tools are name-lookups
  that cannot reach a path. For live-kernel
  schemas, `tests/test_introspect.py` runs the exact probe the kernel runs and proves
  the names-and-dtypes readback never carries a value. For the traceback guard,
  `tests/test_traceback.py` proves a planted secret never survives the rewrite —
  from an exception message, a pasted source line, a frame path, or a workspace
  data file named by a crafted frame — and `tests/test_egress.py` pins that
  nothing outside the egress gateway can reach the sanitiser.
- **Live spike.** `scripts/spike_copilot_chat.py` opens a real session and asks
  the agent to read a file; it has no tool to do so.

## Requirements

The copilot needs the optional extra (`pip install "mooring[copilot]"` — or
`uv add` / `uv tool install` / `uvx`; see
[optional extras](build-and-distribute.md#optional-extras)), a GitHub Copilot
licence (`mooring ai login`), and your organisation's Copilot **CLI/agent
policy** enabled. See [Configuration](configuration.md) for the `[ai]` settings.

Optional **name detection** (the structured-PII guard's NER pass) needs the
separate `pii` extra (`pip install "mooring[pii]"`); without it the guard
still does its stdlib structured-PII scan. See
[Name detection](#name-detection-opt-in-local-ner).
