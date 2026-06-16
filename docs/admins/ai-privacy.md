---
icon: lucide/shield-check
---

# Why the copilot can't see your data

Mooring's AI copilot helps analysts write notebook code while being **structurally
unable to see the data itself**. This page is for analysts who want assurance and
for security reviewers who need to verify the claim. The short version:

> The assistant only ever receives a dataset's **schema** (column names + types)
> and the notebook's **source code**. It has no tool that can read a data file,
> a cell output, or a variable value — and mooring never sends those anywhere.

This structural guarantee covers the **dataset and notebook**. An admin can
additionally opt in to **team context** (instructions + a data dictionary) — text
your team authors. That is a deliberately *weaker*, non-structural channel; it is
off by default and described in [Team context](#team-context-opt-in-not-a-structural-guarantee)
below. When it is on, the headline above holds for your data, but the model also
sees whatever your team wrote into those files.

## What the assistant receives

| Sent to the model | Why it's safe |
|---|---|
| **Schema** — column names, dtypes, row count | Built by `schema.py`, which reads only a parquet footer or a csv/xlsx header. It never materialises a row, so no value is ever produced — proven by the `test_schema.py` "value never leaks" tests. |
| **Notebook `.py` source** | A marimo notebook is pure Python; the data is loaded at *runtime* (`pl.read_parquet(...)`). The source is code, not data. |
| **Your chat messages** | What you type. |

## What it never receives

- **Cell outputs / dataframe previews** — these are where real values appear.
- **Variable values / kernel state.**
- **Error tracebacks** (which can embed values).
- **The contents of any data file.**

## The four structural guarantees

1. **Single choke point.** The context handed to the model is assembled in one
   place (`ai/chat.py:build_system_context`) — from the schema text and the
   notebook source, plus (only when team context is enabled) the team instructions
   and the value-minimised data-dictionary slice. Nothing reaches the model except
   through this one function.
2. **Value-free tools only.** The agent is given mooring's own tools (`ai/tools.py`):
   list datasets, get a schema, read the notebook source, and *propose* a cell —
   each value-free by construction. When a data dictionary is configured, three more
   tools (`list_tables`, `describe_table`, `search_dictionary`) serve it; they look
   up tables by name in an **in-memory parsed index** (never a filesystem path) and
   return only the five allowlisted fields (see [Team context](#team-context-opt-in-not-a-structural-guarantee)).
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
   sees the result.)
4. **marimo's own AI is turned off.** marimo ships a built-in AI assistant that
   *does* send sample values to whatever model it's configured with. Mooring
   disables it in every editor it launches by writing a `.marimo.toml`
   (`ai.enabled = false`, `completion.copilot = false`) into the workspace, which
   marimo reads ahead of any personal config.

Nothing about a conversation is persisted: the session store, telemetry, config
discovery, skills, file hooks, and host-git access are all switched off.

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

## The one thing to watch

Anything **you type into a cell or the chat** is, by definition, visible to the
assistant. If you hard-code a real value into a cell —
`df.filter(pl.col("ssn") == "123-45-6789")` — that literal is part of the source
the assistant can read. The chat reminds you of this; **never paste real values**.
A future release will add an automatic PII/redaction guard on outbound text.

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
  that a secret in an instructions/description field is withheld.
- **Live spike.** `scripts/spike_copilot_chat.py` opens a real session and asks
  the agent to read a file; it has no tool to do so.

## Requirements

The copilot needs the optional extra (`pip install mooring[copilot]` /
`uvx mooring[copilot]`), a GitHub Copilot licence (`mooring ai login`), and your
organisation's Copilot **CLI/agent policy** enabled. See
[Configuration](configuration.md) for the `[ai]` settings.
