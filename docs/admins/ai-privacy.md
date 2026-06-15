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

1. **Schema-only context.** The context handed to the model is assembled in one
   place (`ai/chat.py:build_system_context`) from the schema text and the notebook
   source — nothing else.
2. **Value-free tools only.** The agent is given exactly four mooring tools
   (`ai/tools.py`): list datasets, get a schema, read the notebook source, and
   *propose* a cell. Each is value-free by construction. The session's
   `available_tools` allowlist contains **only** these names, so the SDK's
   built-in file-reading and shell tools are **not available**. The four mooring
   tools run without a permission prompt (they are value-free), while a **deny-all
   permission handler** rejects anything else as a backstop; and the agent runs
   with an **empty working directory** so there are no data files within its reach.
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
  marimo channel is HTTP-only.
- **Live spike.** `scripts/spike_copilot_chat.py` opens a real session and asks
  the agent to read a file; it has no tool to do so.

## Requirements

The copilot needs the optional extra (`pip install mooring[copilot]` /
`uvx mooring[copilot]`), a GitHub Copilot licence (`mooring ai login`), and your
organisation's Copilot **CLI/agent policy** enabled. See
[Configuration](configuration.md) for the `[ai]` settings.
