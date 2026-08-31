---
icon: lucide/shield-check
---

# How Mooring controls what the copilot can see

By default, Mooring's AI copilot helps analysts write notebook code while being **structurally
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

An administrator can separately deploy [trusted AI routing](#trusted-ai-routing).
That mode deliberately permits a firm-approved endpoint to receive customer
information authored in notebook source or chat messages. It still does not expose
cell outputs, dataframe values, dataset rows, or arbitrary files.

## Trusted AI routing { #trusted-ai-routing }

Trusted routing lets analysts keep a broad choice of general coding models without
using a best-effort PII detector as the final routing decision. Before Mooring
constructs any general-provider session, the exact captured notebook context is sent
to a deployment-approved OpenAI-compatible classifier. It returns one value-free
decision:

- `general_ok` — construct the user's selected general coding model;
- `trusted_required` — use the user's deployment-approved coding-model choice;
  uncertainty takes this path;
- `block` — send the content to no coding model.

Every later prompt and changed notebook source is checked again. A conversation can
move from general to trusted but never back, and its prior general conversation is
carried only to the higher-trust destination. Recognized high-confidence credential
and secret patterns are blocked locally before the classifier; this local scan is
best-effort. The traceback guard and server-side Apply/code guard remain unchanged.
The legacy PII prompt hold is disabled for routed chats, removing its false-positive
confirmation flow; the approved classifier becomes the decision point.

The Apply/code guard present in this repository is deterministic static analysis, not
a second model call. If your deployment already adds a separate model-based destructive
reviewer, keep that enforcement boundary in place around Mooring; trusted routing does
not replace or invoke an external reviewer that is not configured in this codebase.

### Two profile sources

A routing profile comes from one of two places, and the managed one always wins.

**Managed** is the `MOORING_AI_*` environment, supplied by a managed launcher, MDM
profile, or equivalent administrator-controlled deployment: an explicit HTTPS endpoint,
classifier model, trusted coding-model allowlist, default model, and dedicated
`MOORING_AI_TRUSTED_API_KEY`. It cannot be created, changed, or overridden from the
Settings page or `config.toml`. `MOORING_AI_ROUTING` is authoritative whenever it is
**present**: a false value switches routing off outright, so a launcher can forbid the
feature without depending on what an analyst's config says.

**Self-configured** is the `[ai.routing]` table in the per-machine `config.toml`, edited
from **Settings → AI copilot**, for an analyst with no managed deployment behind them.
It is validated exactly as a managed profile is — explicit HTTPS with no credentials,
query, or fragment; a classifier; a default model that is a member of its own allowlist;
a credential — and it is refused until all of those are present. Its credential lives in
a **separate** OS credential-store slot (`mooring-openai-trusted`), never the general
OpenAI key and never `config.toml`.

The difference that matters is what the product is allowed to *say*. A self-configured
profile is labelled **Self-configured**, structurally: the label is a constant in
`ai_config.SELF_CONFIGURED_LABEL`, not a value read from config, so a user cannot name
their own endpoint after a firm. Only a managed profile may describe itself as approved,
and the chat chrome words itself from the profile source the server reports — badge,
tooltip, and every route notice. An absent or unrecognised source under-claims rather
than guessing, because falsely asserting that a firm approved an endpoint is the only
direction that does harm.

An administrator who wants the feature gone entirely has two levers, both of which can
only restrict: set `MOORING_AI_ROUTING=0` in the managed environment, or pin
`"ai.routing.enabled" = false` in the synced `mooring.toml`'s `[policy.settings]`. As
with every policy knob, the permissive direction is not expressible — a policy can take
a self-configured profile away, and there is no `mooring.toml` that can hand one out.

Whichever profile is live, the trusted route never falls back to the user's general
OpenAI credential and never follows redirects. The browser receives only a safe profile
label, its source, the approved model IDs, and their default: it never receives the
endpoint, credential, API version, or classifier ID.

Settings may store a user's **default selection within** the live profile: a
customer-data model from the current allowlist and either **Automatic** or **Always use
approved** routing. These are preferences, not trust designations. A hand-edited or
stale model never reaches the provider; Mooring intersects it with the live allowlist
and falls back to that profile's own default. An invalid hand-edited routing
value fails upward to the approved route rather than silently becoming automatic.

The chat UI keeps the general and customer-data model choices separate. Each notebook
inherits Settings unless the user chooses **Override this notebook**. These personal
overrides live only in browser storage, namespaced by an opaque repository identity and
the normalized notebook path; they are not synced team policy. **Automatic** lets the
classifier choose the route; **Always use approved** starts on the trusted route but
still runs local secret checks and honours a classifier `block` decision. There is no
control that can force content onto the general provider, and an upgraded conversation
never moves back. Changing a model or routing preference starts a fresh chat; Settings
changes affect newly opened chats. See
[configuration](configuration.md#trusted-ai-routing).

For the first release, general routed sessions expose only the proposal tool. Mutable
read tools become available after the conversation is on the trusted route, where each
dynamic tool result is checked before the coding model receives it. The proposal tool's
result is also checked while on the general route, closing a race with a notebook changed
during a model turn. Investigate and batch build are disabled for the entire time trusted
routing is enabled.

## What the assistant receives

| Sent to the model | Why it's safe |
|---|---|
| **Schema** — column names, dtypes, row count | Built by `schema.py`, which reads only a parquet footer or a csv/xlsx header. It never materialises a row, so no value is ever produced — proven by the `test_schema.py` "value never leaks" tests. |
| **Live dataframe schemas** — names + dtypes of dataframes loaded in your kernel | Built by `ai/introspect.py`, which runs a **fixed, value-free probe** in your kernel and reads back only names + dtypes. Covers data loaded from *outside* the workspace. Value-free by construction, not by physical impossibility — see [Live dataframe schemas](#live-dataframe-schemas-data-outside-the-workspace). |
| **Notebook `.py` source** | A marimo notebook is pure Python; the data is loaded at *runtime* (`pl.read_parquet(...)`). The source is code, not data. |
| **The notebook catalog** (opt-in) — every notebook's `# H1` title, its imports, and the inputs/checks/SQL tables its **source declares** | A reduced, allowlisted view of the same authored source above — never another notebook's code, never a markdown paragraph, never a cell output, and never a `.mooring/` run receipt. Off by default; fetched on demand through three name-lookup tools. See [the notebook catalog](#notebook-catalog). |
| **Power BI semantic model** — table/column names + types, relationships, measure and calculated-column DAX | Extracted by an **allowlist** parser (`pbip_model.py`) from a synced PBIP's TMDL text: partition/source **M expressions** are skipped *without being captured*, **RLS roles** and **translations** are never opened, annotations and unknown constructs are dropped. DAX is authored code — the same class as notebook source, with the same best-effort scanning caveat. See [the semantic model](#power-bi-semantic-model) below. |
| **Dataset pointer names** — the NAME and file format of each `[datasets]` entry | So the copilot can write `md.path("sales")` wiring for you. Deliberately narrower than the rest of `mooring.toml`: the **location** — the share, the server, the URL — is never sent, because `mooring_datasets` resolves it in your kernel and the model has no use for it (`datasets.copilot_guide`). Names are constrained to a bare `[a-z0-9._-]` token (control characters cannot survive into the prompt) and the format to a short alphanumeric suffix. A URL location carrying a query string, fragment or userinfo is refused from the synced file in the first place — a token hidden in a plain **path segment** is only best-effort scanned; see [Pointing at data too big to sync](../users/daily-workflow.md#pointing-at-data-too-big-to-sync). |
| **Your chat messages** | What you type. The `/explain` walkthrough (and its "Add as notes cell" follow-up) sends **fixed, value-free prompt text** over this same channel — no new egress surface. A pasted **traceback** is rewritten value-safe and held for your confirmation before it can leave — see [Pasted tracebacks](#pasted-tracebacks). |

## What it never receives

- **Cell outputs / dataframe previews** — these are where real values appear.
- **Variable *values*.** Mooring may read a live dataframe's **schema** (names +
  dtypes — see [Live dataframe schemas](#live-dataframe-schemas-data-outside-the-workspace)),
  and — for names it asked about itself — whether a name is **bound** and which of
  sixteen fixed words mooring's own probe **classifies** it as
  ([the probe's second question](#the-probes-second-question-is-this-name-bound)).
  Never a stored value, never anything derived from one, and never a string read off
  the object — a class name included.
- **Raw error tracebacks.** A traceback can embed values (`KeyError: 'ACME Ltd'`),
  and mooring never captures one. Two things can put an error message *near* the
  model, and both are rewritten value-safe first by the same sanitiser: an analyst
  can *paste* a traceback into the chat (rewritten and held for an explicit confirm;
  the raw paste is never stored, so no code path can forward it), and the
  **Run & report** path can run the notebook and read only marimo's own error lines —
  started by your click, or, when
  [`[ai] auto_run_report`](#apply-gate) is on, by mooring itself after a change the
  model wrote did not complete. What survives the rewrite is best-effort, not
  structural — see [Pasted tracebacks](#pasted-tracebacks) and
  [Run & report](#run-and-report) for the exact contracts.
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
   list datasets, get a schema, read the notebook source, and *change* the notebook —
   each value-free by construction. There is exactly **one** write tool, and it covers
   every change the copilot can make (new cells, edits, deletions, a wholesale rewrite)
   as one patch. It is registered under one of **two** names, chosen per session by
   [`[ai] auto_apply`](#apply-gate): `mooring_propose_notebook_edit` when the analyst
   applies (it emits a card and writes nothing), and `mooring_edit_notebook` when the
   change lands inside the call. Same handler, same JSON schema, same checks — the name
   differs because it is an instruction to the model about what happens next. It
   answers with mooring's **static
   check** of the notebook that proposal would produce (`marimo_rt.validate_notebook_source`):
   the candidate is composed in memory, never written, and checked on the AST alone —
   nothing is executed, so there is no runtime value for a finding to carry. A finding is
   a rule code, a rule slug, a line number, and the rule's own wording. That text is the
   one thing here mooring does not author (marimo's `message`/`fix` are forwarded as
   written), so it goes through `egress.scrub_text` like every other tool result before it
   can leave. An edit or a deletion is checked once more before that: the model has to
   state which cell it *believes* is at the index it is targeting, and mooring refuses
   the whole change unless that description fits the targeted cell **and no other cell
   whose source differs**. Both halves are load-bearing — marimo writes every markdown
   cell with the same opening line, so a one-line description on its own would fit any of
   them and prove nothing. What this establishes is that the model knows what is at the
   index it named; it does not establish that the model *read* it, which no static check
   can. The refusal tells it to re-read or to describe the cell more fully; it never
   quotes back what is actually there.
   When a data dictionary is configured, three more
   tools (`list_tables`, `describe_table`, `search_dictionary`) serve it; they look
   up tables by name in an **in-memory parsed index** (never a filesystem path) and
   return only the five allowlisted fields (see [Team context](#team-context-opt-in-not-a-structural-guarantee)).
   When the opt-in [notebook catalog](#notebook-catalog) is enabled, three more
   (`list_notebooks`, `search_notebooks`, `describe_notebook`) serve it the same way —
   name lookups in a pre-parsed in-memory index, returning metadata only, never another
   notebook's code.
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
   When a cell is applied — by your click, or, with
   [`[ai] auto_apply`](#apply-gate) on, by the model inside its own tool call — mooring
   writes the cell's **source code** into
   the notebook's `.py` file (via marimo's own codegen); the editor, launched with
   `--watch`, reloads and runs it (unless you have turned
   [*Run an applied cell straight away*](#apply-gate) off, in which case it reloads
   the cell marked stale and runs nothing). mooring never reads cell outputs, and never
   connects a marimo *websocket* — and outputs, dataframe previews, and variable
   values are delivered *only* over that websocket. So a **value** cannot travel back
   through mooring to the model. (The cell runs in *your* kernel; only your browser
   sees the result.) Live-schema introspection ([below](#live-dataframe-schemas-data-outside-the-workspace))
   keeps this invariant: it pushes a fixed probe in over HTTP and reads back only a
   names-and-dtypes file that probe wrote — never a cell output, never the websocket.

   What has changed with `auto_apply` is that the model now learns **that** a cell ran,
   not what it produced. A write it makes itself comes back as an **observation** built
   from that same probe: which of the names the change should have bound are now bound
   and a one-word classification of each from mooring's own fixed vocabulary, plus the
   names + dtypes + row count of *those* dataframes, and mooring's own words for the
   status. It is the live-schema channel, asked a second question — no output, no repr,
   no value, no websocket — and it is governed by the same `[ai] live_schema` switch,
   so a machine with live schema off gets "could not see it run" rather than a kernel
   read through another door. That is what lets the model correct its own mistake in the
   turn instead of handing you a broken cell; see
   [the probe's second question](#the-probes-second-question-is-this-name-bound) for the
   exact shape and its boundary.
4. **marimo's own AI is turned off.** marimo ships a built-in AI assistant that
   *does* send sample values to whatever model it's configured with. Mooring
   disables it in every editor it launches by writing a `.marimo.toml`
   (`ai.enabled = false`, `completion.copilot = false`) into the workspace, which
   marimo reads ahead of any personal config.

Nothing about a conversation is persisted: the session store, telemetry, config
discovery, skills, file hooks, and host-git access are all switched off.

## Applying a cell: the check, and whether it runs { #apply-gate }

Apply is the one moment the copilot's code touches your machine, so four settings
govern it. All four live on the hub's **Settings** page under *AI copilot*, and all
four can be pinned by a [team policy](policy.md).

**`[ai] apply_guard` (default `true`) — read the cell before it lands.** mooring
scans every proposed cell and holds the ones Undo cannot take back. Undo restores
the notebook's *text*: a complete remedy for a cell that loads a dataframe and
draws a chart, and no remedy at all for one that deleted a file, ran a program, or
dropped a table. So ordinary work — reading data, dataframe code, plots, writing a
new file into the outbox — applies silently, and only the irreversible things ask,
in plain English and with the offending line named. The check runs on the server
side of the Apply, inside the same lock as the write and re-derived from the ops
every time, so a client cannot claim to have shown you a dialog it did not show.
Turning it off applies every proposed cell with no prompt.

Like every other detector here it is **best-effort defence in depth, not a
guarantee**: a cell that applies silently has not been proven safe. The classifier
reads names, so code that reaches the same call another way slips past — `rm =
os.remove` then `rm(p)`, `mod = os` then `mod.remove(p)`, `os.__dict__["remove"](p)`,
and `p.rename(q)` on an unresolved receiver all classify clean. These are stated in
`ai/codeguard.py`'s own docstring and are accepted limits, not oversights: following
arbitrary rebinding needs dataflow analysis the module deliberately does not do. The
gate is built for an honest model that makes a mistake and an analyst who cannot read
the diff — not for code written to evade it.

The scan is **value-free** like everything else here: a finding is a line number, a
fixed kind, and a fixed label — never a path, a name, or a fragment of your code.
The scan itself is local: it reads code already on your machine, and no code, path
or finding label leaves it.

One thing does leave, and it is worth stating precisely rather than glossing. If
your admin has configured `[logging] endpoint`, each Apply emits a telemetry line
carrying the verdict's **band** and the **number** of findings — `band="floor",
findings=2` — for held, confirmed and clean applies alike. Never the kinds, never
the labels, never a line number, never anything from the cell. That is deliberate
(`hub/routes/chat.py`: *"Count + band only: the central sink never carries kinds"*),
and the value-free kinds go to the **local** activity ledger instead.

That holds for **every** Apply, including the ones the model makes itself. The two
ledgers are filled by the writer (`app/auto_apply.py`), not by the Apply button's route,
so a change that lands inside a tool call is as visible in your telemetry and your local
activity journal as one you clicked — same event names, same split. (It was not, briefly:
when auto-apply first landed, both were still emitted only by the route, so the
commonest Apply in the product recorded nothing. Fixed, and pinned by tests.)

**`[ai] apply_runs` (default `true`) — does an applied cell run?** By default Apply
means *add and run*: mooring writes marimo's `runtime.watcher_on_save = "autorun"`
into the workspace's `.marimo.toml`, so the `--watch` reload runs the cell that just
landed and you see its code and its result together. Set it `false` and mooring
writes marimo's `"lazy"` instead: the cell arrives in your notebook marked **stale**
and nothing executes until you press run. Slower, and worth it for a team that wants
a human between the model's code and the kernel every time.

With it off, that human stays between them **all the way**, which settles two things
about the loop below. Nothing has run, so mooring reports that it could not see
anything — never a verdict, and in particular never "the code that defines them did not
run to completion", which would be flatly false about a cell nobody has run yet. And
`auto_run_report` cannot fire either: re-running the whole notebook to diagnose a cell
you deliberately staged would defeat the setting that staged it.

**`[ai] auto_apply` (default `true`) — who presses the button.** By default a change
the copilot writes lands as soon as it is written, and mooring hands the model back a
value-free observation of what happened, so it can see its own mistake and fix it in
the same turn instead of waiting a round trip for a human to click. This does **not**
widen what may land: `apply_guard` above still reads every proposed cell first and
still holds anything Undo cannot take back for your explicit confirm — the
irreversible cells still stop and ask. What it removes is your look at the *ordinary*
ones before they land, and Undo remains the remedy for those by construction. Set it
`false` and the copilot goes back to proposing: nothing touches the notebook until you
press **Apply**. That is the setting for a team that wants a human decision on every
write, and a policy may pin it.

**`[ai] auto_run_report` (default `true`) — may mooring re-run your notebook?** When
the observation says an applied cell did not complete, mooring may run the same
value-free smoke path described under
["Run & report"](#run-and-report) itself, so the failure reaches the model without you
relaying it. Nothing about *what* is read changes — the same closed error taxonomy, the
same unconditional sanitiser, the same value-free receipt — but it re-executes the
notebook without you asking, which is why it is a switch of its own rather than a
detail of `auto_apply`. Set it `false` and the model is still told the cell did not
complete; mooring just will not re-run anything on your behalf, and **Run & report**
stays the button it always was. When it does fire, the summary goes through the same
[outbound PII valve](#structured-pii-pre-flight-scan-opt-in-best-effort) your own turns
do, and **fails closed**: where block mode would hold the text for your confirmation,
nothing is sent at all (there is no-one at a tool result to press "Send anyway"), the
model is told to ask you instead, and you are told why. Whatever *is* sent appears in
your transcript, verbatim.

A policy may pin `apply_guard = true`, and `apply_runs`, `auto_apply` and
`auto_run_report` to `false` — the strict end of each. It cannot pin any of them the
other way: there is no policy that disarms the check, none that makes a teammate's
applied cells run, none that takes a teammate's Apply button away, and none that makes
mooring re-run their notebook.

One nearby setting is **not** part of this gate and is not policy-governed:
`[ai] max_tool_iters` (default `200`) is a ceiling on how many tool calls the model may
make within one turn — a backstop against a runaway loop, not a work budget. It is set
high on purpose, because the control for "that is enough" is the chat's **Cancel**
button, not a small cap that stops a long analysis mid-thought. Raising or lowering it
changes nothing about what the model sees or what may land; a value below 1 is ignored
(it would end every turn before the first tool call). See
[the policy reference](policy.md) for why an integer is deliberately outside the
policy model.

### The honest limit of auto-apply { #the-honest-limit-of-auto-apply }

Everything above is about what mooring *reads*. There is one thing `auto_apply` changes
about the model's own reach, and it deserves saying plainly rather than being left to be
discovered.

Without it, the model can propose code that a human runs. With it, the model can **run
code** — inside the apply gate, which still holds anything Undo cannot take back, but
without a person reading the ordinary cells first. That matters here because several of
the value-free readbacks on this page report **names**, and names are strings that
running code can choose. A cell can bind a dataframe to a variable named after a value
(`globals()[str(value)] = df`) or rename a column to one (`df.rename(...)`), and those
names are exactly what the live-schema channel reports back. The channel is not new —
it is the same one an analyst has always been able to point at a pivot table — but the
human click that used to sit in front of it does not.

What mooring does about it, and what it does not:

- **It does not claim a filter fixes this.** A name is arbitrary text by design; no
  scanner makes arbitrary text value-free. The [PII scan](#structured-pii-pre-flight-scan-opt-in-best-effort)
  still runs over column names and still withholds a well-formed one, and that remains
  what it has always been — a floor, not a guarantee.
- **It narrows the surface where narrowing is free.** The observation a model-written
  change gets back reports the frames it *asked about* and no others (it used to also
  list every frame in the session), and its one classification field is a closed
  vocabulary rather than a string read off the object — see
  [the probe's second question](#the-probes-second-question-is-this-name-bound).
- **It gives you the switch that removes the premise.** `[ai] auto_apply = false` puts
  the human click back in front of every write; `[ai] live_schema = false` stops the
  kernel being read at all. Both can be [pinned by policy](policy.md), and a policy can
  only ever pin them off. For a repo where this class of risk is unacceptable, that
  pair — not a detector — is the control.

## Parallel "investigate": read-only sub-agents (on by default) { #investigate }

With trusted routing, investigate is unavailable for the entire conversation in this
first release. This avoids creating branch prompts outside the per-turn routing gate.

On by default (opt out with `[ai.investigate] enabled = false`, or
`MOORING_AI_INVESTIGATE=false`). It defaults on because it adds **no** data surface — it
is structurally value-blind, like the semantic-model reader — but each branch is a full
model session, so turn it off (or lower `max_concurrency`) if you want to avoid the extra
spend, which is heaviest on the Copilot provider (a subprocess + premium request per
branch). When on, the copilot gains one extra tool, `mooring_investigate`, that lets it
research several **independent** sub-questions **in parallel** before it proposes — e.g. "understand these three
notebooks" or "map these two semantic models". It runs automatically as part of
answering; there is **no separate approval step**, because the only thing a human ever
approves stays the same: **the code you Apply**. Each branch spawns a **read-only**
sub-agent, their value-free findings are merged, and the copilot turns them into one
proposal you review and Apply.

It preserves every guarantee above, and its safety rests on one load-bearing invariant:

- **The sub-agents are read-only — this is the guarantee, not a nicety.** A branch's
  finding is the sub-agent's *own* answer, returned to the main copilot as a tool result.
  That answer is trusted to be value-free because the sub-agent is **structurally
  value-blind**: it is built with **no propose, edit, or any value-returning tool** — only
  the value-free read tools (schema, notebook source, dictionary, semantic model, and —
  when it is enabled — the notebook catalog, since "which notebook already does this?" is
  exactly a branch's job). Note the compounding: a fan-out runs up to 8 sub-agents at
  once, so anything they can read is read in parallel. That is a large part of why the
  catalog carries no free prose and is opt-in. The
  read-only tool subset is enforced in one place (`ai/tools.py`: the one write tool is
  gated on the two proposal callbacks *and* the apply callback, none of which a sub-agent
  is ever given — so it registers under neither of its names) and pinned by a test.
  The merge still applies the checksum-PII floor as defence-in-depth, but that floor is
  *beneath* the structural guarantee, not the guarantee itself.
- **Investigations cannot recurse.** `mooring_investigate` is never in a sub-agent's own
  toolset (a read-only session drops it), so depth is bounded to 1.
- **No new egress channel.** Findings ride the **existing** tool-result path
  (`egress.to_openai_tool_message`) — the same channel every tool result uses — and every
  sub-agent's context comes from the **same** single assembler (`build_system_context`).
- **A checksum-PII sub-question is blocked, never sent.** Each branch's question passes the
  value-free PII pre-flight before any session opens; there is no human at a sub-agent, so a
  block-mode hold blocks that branch rather than being auto-confirmed.

Because each branch is a full model session against your account's quota, it is capped by
`[ai.investigate] max_branches` (default 8) and `max_concurrency`. `max_concurrency`
defaults to `0` = **auto**, resolved per provider because the per-branch cost differs by an
order of magnitude: **2 on Copilot** (each branch is a ~150 MB CLI subprocess *and* a
premium request) and **6 on OpenAI/LiteLLM** (each branch is just an HTTP stream). Any value
above `0` is an explicit override and wins for every provider.

While a fan-out runs, the chat streams an inline progress cue on the tool line
("investigating · researched 2 of 3…"). That cue carries **counts and statuses only** —
never a sub-question or a finding — and it goes to your browser, not to the model.

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
model = "gpt-5.1"

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

## The notebook catalog (opt-in): what the repo's other notebooks are for { #notebook-catalog }

The copilot can read **one** notebook: the one you have open. But the question a growing
team actually asks is *"has someone already built this?"* — so mooring can additionally
give it a **catalog** of every marimo notebook in the workspace, reachable through three
on-demand tools (`mooring_list_notebooks`, `mooring_search_notebooks`,
`mooring_describe_notebook`).

It is **off by default** (`[ai] notebook_catalog = false`), in the same opt-in tier as
[team context](#team-context-opt-in-not-a-structural-guarantee) and the
[code library](#code-library-reusing-your-teams-helper-functions-opt-in) — *not* the
on-by-default tier of the [semantic model](#power-bi-semantic-model). Two reasons, and
they are the honest ones:

- It **widens the model's view from one notebook to the whole repo**. Every other
  on-by-default source is scoped to what the analyst already chose to open.
- It carries **one authored-prose field** (the title). The semantic-model extractor, by
  contrast, has *no* free-prose field at all — it drops TMDL `///` doc comments — so its
  default-on posture does not transfer here.

Per notebook, the catalog holds exactly: its **path**, its `# H1` **title**, the dotted
**names** of what it imports, the `name`/`path` **string literals** it hands
`mooring_inputs.fingerprint`, which `mooring_checks` function it calls plus that check's
literal `name=`, and the identifier-shaped **table names** its `mo.sql` queries select
FROM. What keeps it there:

- **An `ast` allowlist, never an execution.** `notebookindex/` parses the `.py` text; it
  never imports, runs, or `literal_eval`s a notebook (pinned by a canary test that plants
  a file-writing side effect and proves it never fires). A cell **body**, an
  **expression**, an arbitrary **literal**, and a cell **output** have *no slot* in the
  model — they are structurally impossible to send, not merely filtered.
- **A markdown paragraph has no slot either.** Only a `# H1` heading is taken from a
  markdown cell; the prose beneath it is discarded. This is deliberate and was learned the
  hard way in review: analysts paste result tables, closing balances, and account names
  into markdown ("Top account …", "| EMEA | 4,231,999 |"), and no scanner makes arbitrary
  prose value-free. There is also **no fallback** to "the first line if there's no
  heading" — the hub's *display* title has one, which is exactly how a pasted table row
  would otherwise have become a title.
- **A literal is lifted only from a named slot of a known call.** A *computed* argument —
  an f-string, a variable, a concatenation — is dropped rather than captured, because a
  runtime-built string is exactly where a data value would appear.
- **SQL strings and comments are stripped before table names are read.** Scanning a raw
  query would be a leak, not a nicety: real narrative text contains "from …"
  (`where narrative like '%transfer from ACME_Holdings_Ltd%'`), and those account names
  would be reported as tables. Quoted strings, dollar-quoted blocks, quoted identifiers,
  and both comment forms are removed first, and an *unbalanced* quote truncates the rest —
  losing a table name is the correct trade.
- **A run receipt is never opened.** `.mooring/inputs` and `.mooring/checks` record what a
  run against **real data** observed; the catalog reports only what the **source
  declares**. Routing a receipt to the model would be a new egress channel, so no code
  path here can reach one — and `.mooring/` (which also holds undo snapshots of real
  notebooks) is excluded from discovery twice over.
- **No tool returns another notebook's source.** Only the open notebook's code is
  readable (`mooring_read_notebook_source`); a second notebook's full authored code would
  be a new, unreviewed egress and is deliberately not served.
- **The per-notebook opt-out applies here too.** A notebook the team
  [turned AI off for](#turning-the-copilot-off-for-a-notebook) is dropped from the catalog
  before the tools are built, so fencing one off also removes it from search.

**The honest classification:** the one authored-prose slot is the **H1 title** — the same
best-effort tier as a code-library docstring or a dictionary description. It is capped at
120 characters, scanned with the **full** prose scanner (including the optional
[local-NER name pass](#name-detection-opt-in-local-ner) when you have enabled it), and
withheld **whole** on a hit — but a plain-text value a regex and NER both miss can
survive, exactly as one typed into a notebook cell can. Everything else is structural.

Note the **next-open semantics**, as for the semantic model: the catalog is a snapshot
taken when a chat opens, so a notebook fenced off *mid-session* stays searchable in an
already-open chat until it is reopened. New chats respect the toggle immediately.

The same value-free entries feed the hub's own **search box** — but that path is local: it
is matched in your browser, goes nowhere, and is **not** gated by this setting. Turning
`notebook_catalog` off returns the *assistant* to seeing only the notebook you have open;
your search box keeps working.

Run **`mooring catalog [terms…]`** to see exactly what the catalog tools would emit —
*offline*, before the copilot ever reads it. It applies the same per-notebook opt-out the
chat path does, so the preview is not a superset of what the model can see.

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

That switch governs **every** read of your kernel, not just the schema in the chat
context: the [observation](#apply-gate) a model-written change gets back is this same
probe asked a second question, so with live schema off it is not run either — the model
is told mooring could not see the change run. A single switch that means what it says,
rather than one path that respects it and another that does not.

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

### The probe's second question: "is this name bound?" { #the-probes-second-question-is-this-name-bound }

The same frozen probe answers a **second** question, used only by the observation a
model-written change gets back (see
[guarantee 3](#the-four-structural-guarantees) and [`[ai] auto_apply`](#apply-gate)):
*are these particular names bound in the kernel, and what are they?* Its readback adds
one section, `names`, holding one entry per name mooring asked about:

- **`present`** — a bool. Bound in the kernel globals, or not.
- **`kind`** — one word from a **closed vocabulary mooring wrote**: `dataframe`,
  `lazyframe`, `series`, `str`, `int`, `float`, `bool`, `bytes`, `list`, `dict`,
  `tuple`, `set`, `none`, `function`, `class`, or `other`. The probe *classifies* the
  object — by identity against real type objects (`type(x) is str`, `type(x) is
  polars.DataFrame`) — and answers with its own constant. **Nothing read off the object
  is ever part of an answer**: not `repr(obj)`, not `str(obj)`, not a length, a
  row/element count, a dict key or an attribute walk — and not the class name either.

That last exclusion is the point, and it is a correction. This field used to report
`type(obj).__name__`, described as "the identifier from a `class` statement". It is not:
`__name__` is a **writable** class attribute, and can be a metaclass property computed
at read time — so it is an arbitrary string the executing cell chooses, ~64 characters
per asked name, with no cap on the number of names. A cell doing
`c = type("T", (), {}); c.__name__ = "c" + chunk` scans **clean** under the
[Apply check](#apply-gate), so with `auto_apply` on no human need ever read it, and a
confidential row could be chunked across a handful of names and reassembled by the
model. No reader-side filter on a free string closes that ("an identifier of sane
length" passes base32 and raw text alike); a closed vocabulary does, because the answer
no longer depends on the object's own strings. A subclass — of anything, including a
real dataframe — lands in `other` rather than borrowing a label.
`tests/test_introspect.py::test_a_class_name_cannot_be_used_to_smuggle_a_value_out`
runs that attack, with an assigned `__name__` and a metaclass property, and proves both
land in the catch-all.

The names mooring asks about are **its own**: they come from static analysis of the
cell it just wrote (`marimo_rt.cell_defs`), so the asker already knew them before the
probe ran, and a name it did not ask for is dropped on the way back. Cell-local
(`_`-prefixed) names and anything that is not a plain identifier are filtered out
*before* the probe is built. The reader (`_parse_names`) is fail-closed in the same way
`_parse_frames` is, plus one extra lock: a `kind` that is not a member of that fixed set
becomes `other`, while `present` survives — failing a kind closed must not also lose the
fact that the name is bound.

The observation reports the **dataframe schemas of the names it asked about, and no
others.** The session's other frames still reach the model, but through the
[live-schema channel](#live-dataframe-schemas-data-outside-the-workspace) above, which
`[ai] live_schema` governs — and which also governs this probe, since it is the same
one: with live schema off, a model-written change gets "mooring could not see it run",
not a kernel read by another route.

State the remaining boundary honestly rather than overselling it. A *variable name* and
a *column name* are strings the executing code can set from data (`globals()[value] =
df`, `df.rename(...)`), and with `auto_apply` on, the code that executes is code the
model wrote. That is a real channel and it is not closed by any filter — see
[the honest limit of auto-apply](#the-honest-limit-of-auto-apply), and the same point in
the [threat model](threat-model.md).

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

An offered folder may sit at **any depth** — `reports/finance/context` is as valid as a
top-level `context/`. Curate a nested one by expanding (or drilling into) it in the hub's
file tree and clicking its **"AI context"** toggle, or by naming the full workspace-relative
path: `mooring ai context add reports/finance/context`. Depth was never the risk; leaving
the workspace is, so an entry that escapes it — `..`, or an absolute path like `C:/secrets` —
is **refused on write and ignored on read**, in `mooring.toml` exactly as in `[sync] folders`.
An offer nested inside a folder that already syncs does not widen the sync scope; it is
already covered.

One honest deviation to note: because the whole offer syncs for any consented teammate,
an offered folder rides `pull` to a teammate who has **not** subscribed to it — harmless
value-free files on disk that never enter the model's context. (`context` off is still
"neither read nor synced".)

### Code library: reusing your team's helper functions (opt-in)

With `[ai] code_index = true` (**off by default**), the copilot can **discover and
reuse** your team's own helper functions/classes — the importable `.py` modules under
your synced folders — instead of re-implementing them. mooring extracts each module's
**API skeleton** with Python's `ast` and feeds the model only that.

- **It is parsed, never imported or executed.** `ast.parse` reads the text; nothing in
  the extractor imports the module, runs it, or evaluates a literal — so a module with an
  import-time side effect is analysed without triggering it (pinned by a canary test).
- **What the model sees is a value-free skeleton, by construction:** function/class/method
  **names**, **signatures** (a present default renders as `...`, never its value), **type
  hints** (string/number constants inside them are blanked), decorator/base **name-heads**
  (call arguments dropped), and each symbol's **docstring** — plus the exact
  `from pkg.mod import name` line to reuse it. A function **body**, any **literal**, a
  **default value**, and a **constant value** have *no slot* in the model — they are
  structurally impossible to send, not merely filtered. This does not rely on the
  PII scanner below (which catches only well-formed identifiers).
- **The one weaker slot is the docstring** — prose your team wrote, the same best-effort
  tier as a data-dictionary description. It is scanned and withheld on a high-confidence
  hit, but a plain-text value a regex can't match can survive. Docstrings live in your
  own reviewed source, so treat them like the code they document.
- **There is deliberately no "read the source body" tool.** Real function bodies are a
  general value channel no scanner can make value-blind, so v1 exposes only the skeleton
  (three value-free tools: `list_helpers`, `describe_helper`, `search_helpers`).

A per-module opt-out lives in the synced `mooring.toml` (`[ai] disabled_code_modules`).

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
inside a library frame. Mooring never captures a traceback (it reads no cell
outputs and never opens the marimo websocket) — a paste is the only way one can
reach the model at all, and that paste no longer travels raw. (The one *error
message* mooring does read is marimo's own failure line from a run you asked for;
it goes through this same rewrite — see [Run & report](#run-and-report).)

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
  session (the dataset schema, the live-kernel schemas, and the captured notebook
  source). The message rescue never re-reads the mutable notebook merely to treat a
  newly added token as already shown.
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

## "Run & report": telling the assistant a cell actually broke { #run-and-report }

The copilot's static checks catch a cell that will not parse, a duplicate definition,
a cycle. They structurally *cannot* catch a wrong column, a mis-called API, or a name
that only resolves at runtime — seeing those means **running** the cell, and mooring
never opens a marimo websocket. So after you Apply a change, the proposal card offers
one button, **Run & report**, and the card says what it will do before you press it.

It is worth being precise about what that button is, because it is the one place
mooring reads an error message rather than being handed one:

- **It is the Verify run, not a new one.** It executes the same `marimo export html`
  smoke run behind the
  [trust badge](../users/daily-workflow.md#verifying-a-notebook-runs) — the same workspace
  run lock, the same value-bearing render written under `.mooring/` and deleted on
  every path, the same process-tree kill, and the same value-free receipt. Your row
  badges from it exactly as a hand-run Verify does.
- **It fires from your click, or from the one setting that says otherwise.** The run
  re-executes *every* cell, which is precisely what the [apply gate](#apply-gate) exists
  to keep deliberate. It is never reachable from a timer or from opening a page. The one
  path that does not begin with the button is
  [`[ai] auto_run_report`](#apply-gate) (default `true`), which lets mooring start this
  same run itself when an applied cell did not complete — set it `false`, or have your
  team pin it `false`, and the button is once again the only way in.
- **Only two things are read from the run.** marimo's stderr is not a log: the exporter
  echoes each cell's own `print` output onto it, so a printed dataframe lands there in
  full. Mooring therefore reads *only* the lines matching marimo's own closed error
  taxonomy, and from each takes the error **kind** (returned as a fixed constant, never
  as text lifted from the stream) and that one line's **message**. The console half has
  no representation at all and no code path can reach it.
- **The message goes through the traceback sanitiser** — `egress.sanitize_traceback`,
  the same one gateway a pasted traceback uses, with the same rule: it survives only if
  it is a fixed interpreter message or every quoted token in it is already in text the
  model has seen this session. So `'revenue'` survives when `revenue` is a column in
  your notebook (which is exactly what makes the loop useful), and `'ACME Ltd'` becomes
  `<redacted: 10 chars>`. Unlike the paste guard, this rewrite is **unconditional**:
  `[ai] traceback_guard = false` does not turn it off, because that switch is about text
  *you* wrote and can see, and here you never see the raw message at all.
- **You are shown exactly what was sent**, in the transcript, together with a value-free
  count of what was withheld. There is deliberately **no second confirm**: you clicked a
  button that said it would send a sanitised error summary, and a confirm card asking you
  to approve a rewrite of text you never saw would be a rubber stamp — it would train the
  reflex the apply gate works to break, without giving you anything to check it against.
  The [structured-PII guard](#structured-pii-pre-flight-scan-opt-in-best-effort) still
  applies to the summary like any other turn, and in block mode still holds it.
  Both halves of that hold for the **automatic** run too, and neither comes for free
  there — an automatic report is a tool result, not a turn, so it passes through neither
  the transcript nor `send`'s valve by itself. So mooring runs the same valve explicitly
  and puts the summary in your transcript explicitly. The one difference is what a block
  means: with no-one to press "Send anyway", a hold **stops** the report rather than
  waiting on it. Nothing reaches the model, you are told the guard held it, and the model
  is told to ask you what the error says.
- **The per-notebook opt-out is re-checked immediately before the send**, not just at the
  click: the run takes minutes, and a teammate's sync or a hub toggle landing inside that
  window stops the report.

Two honest limits. There is **no cell index** — marimo's stderr does not carry one, and
the only place it exists is the render, which is value-bearing and never parsed — so the
assistant is told to locate the cell from the source it already has. And a message that
cannot be proven value-free arrives as `<redacted: N chars>`, which is less useful than
the real text; that is the trade this feature deliberately makes.

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
  that cannot reach a path. For the notebook catalog,
  `tests/test_notebookindex.py` and `tests/test_ai_catalog_tools.py` plant the sentinel in
  a cell body, a filter literal, a computed path, a **markdown paragraph** (including a
  pasted result table), and a **SQL narrative**, and prove none reaches a tool result;
  that a receipt and an undo snapshot on disk are never read; and that extraction never
  executes the notebook. They also pin the *limits* of the claim rather than overstating
  it: the H1 **title** is scanned, not structural, so its test asserts only what the
  scanner actually catches and is labelled best-effort. For live-kernel
  schemas, `tests/test_introspect.py` runs the exact probe the kernel runs and proves
  the names-and-dtypes readback never carries a value — including the attack that made
  the *kind* field a closed vocabulary: it plants a chunked confidential row in a set of
  writable `__name__`s (and one computed by a metaclass property) and proves every one
  lands in the catch-all. `tests/test_observe.py` covers the other half of that path:
  that a verdict — either way — needs positive evidence the reloaded cell actually ran,
  so a stale look at the namespace as it was *before* an edit is never reported as the
  result of the edit, and that the observation reports only the frames it asked about. For the traceback guard,
  `tests/test_traceback.py` proves a planted secret never survives the rewrite —
  from an exception message, a pasted source line, a frame path, or a workspace
  data file named by a crafted frame — and `tests/test_egress.py` pins that
  nothing outside the egress gateway can reach the sanitiser. For
  [Run & report](#run-and-report), `tests/test_run_report.py` drives the whole chain
  against a faked marimo run whose stderr carries a printed dataframe beside the error
  line, and proves that neither the printed values nor the raw message reach the
  session — while a message made of tokens the model has already seen still does.
  `tests/test_auto_apply.py` pins what the **automatic** version of that run owes you:
  that it goes through the same outbound PII valve and sends nothing when block mode
  would hold it, that whatever it does send appears in your transcript verbatim, that a
  staged (`apply_runs = false`) or unobserved change never starts one at all, and that
  the model's own Apply fills the same telemetry and activity ledgers your click does.
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
