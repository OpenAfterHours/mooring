---
icon: lucide/bug
---

# Value-safe traceback fixer

!!! success "Status: implemented"
    All four phases shipped 2026-07: the fail-closed sanitiser (`ai/traceback.py`,
    pure stdlib) behind the single `egress.sanitize_traceback` gateway (one-gateway
    import rule pinned in `tests/test_egress.py`, module covered by the
    `.importlinter` spaCy contract); the chat valve as ONE combined hold in
    `ChatBroadcaster._pii_gate` — sanitise, PII-scan the *sanitised* text, store
    only the rewrite in `_pending`, broadcast a `traceback` SSE event
    `{preview, redactions, pii_findings, token}` — so the existing
    `send_confirmed` path can only ever forward sanitised text and no send-raw
    code path exists; the guard config threaded like `PiiConfig`
    (`open_chat(traceback_guard=…)` → session ctor → `configure_traceback_guard`,
    **not** armed from a hub route as this page originally sketched); the
    `[ai] traceback_guard` default-ON key (+ `MOORING_AI_TRACEBACK_GUARD`, flat
    accessor, weakening-confirm SettingSpec); the batch worker auto-confirming a
    `traceback` hold ONLY when the PII scan of the sanitised text did not itself
    hold — the sanitiser rewrites just the traceback block, so block-mode PII in
    the brief's surrounding prose blocks the job (`pii_blocked`) exactly like a
    plain `pii` hold unless the analyst forces it ("Build anyway"); without the
    auto-confirm a traceback-bearing brief would hang to timeout — recording
    value-free redaction counts on the job result; the "Send sanitised" hold card
    (`chat.js` + a pure `ChatCore.tracebackHoldSummary` formatter, deliberately
    with no send-raw button); the phase-3 known-token rescue (live schema +
    system context + on-disk notebook source); and the offline
    `mooring ai traceback check` CLI. The workspace source re-read is restricted
    to paths resolving UNDER the workspace that end in `.py`, replaces only a
    source line the paste itself showed (a frame pasted without one gets none
    inserted — the sanitiser never ADDS text), and emits the disk line only when
    the frame's line number exists and the line is code-shaped — a crafted frame
    naming a workspace CSV (or any un-shown `.py` line) cannot turn the sanitiser
    into a value channel
    (pinned with a `SECRET_VALUE_DO_NOT_LEAK` fixture in `tests/test_traceback.py`).
    `docs/admins/ai-privacy.md` now documents the sanitised-traceback channel
    honestly in place of the old "never receives tracebacks" claim.

## Problem

When a cell errors, the single most tempting act is to paste the traceback into
an AI. Tracebacks embed data values — `KeyError: 'ACME Ltd'`,
`could not convert string to float: '£1,234'`, a repr of the offending row in a
library frame — so that one paste quietly defeats the copilot's value-blindness.
[Why the copilot can't see your data](../../admins/ai-privacy.md) lists error
tracebacks under "what it never receives" precisely because they carry values;
today mooring keeps that promise only for text *it* assembles, not for text the
analyst types.

The analyst is left with the worst of both worlds. The honest options are "don't
paste it" (so no help at the exact moment help is most needed) or "paste it
anyway" (and hope the opt-in structured-PII guard in `ai/pii.py` catches it —
it catches cards/IBANs/NHS numbers/emails/NINOs, not a customer name inside a
`KeyError`). Meanwhile the copilot already has everything needed to *fix* most
errors — the notebook source, the dataset schema, and the live-kernel dataframe
schemas from `ai/introspect.py` — if only the error could reach it safely.

"Debug from a traceback without the model seeing your data" exists nowhere
else, and it fits mooring's audience exactly: analysts who hit
`ColumnNotFoundError` and `SchemaError` daily and are not going to read a
polars stack trace themselves.

## Design

A traceback never reaches mooring on its own — mooring reads no cell outputs and
never opens the marimo websocket (the only channel that carries them), and this
feature deliberately keeps it that way. The traceback arrives the only way it
can: **the analyst pastes it into the chat prompt**. Auto-capturing the last
exception from the kernel is explicitly rejected — the sidecar probe channel in
`ai/introspect.py` is sanctioned for names+dtypes only, and a traceback is a
value-bearing artifact.

When a pasted prompt contains a traceback, the outbound valve **structurally
rewrites it, fail-closed, before any egress**:

- **Detection.** A block anchored by `Traceback (most recent call last):` (or
  `File "…", line N, in …` frame lines followed by an exception line) is treated
  as a traceback. Chained-exception separators (`During handling of the above
  exception…`, `…direct cause…`) are recognised and kept — they are fixed
  stdlib strings.
- **Exception type: kept.** `polars.exceptions.ColumnNotFoundError` is a code
  identifier, not data.
- **Frames: kept only when they resolve into the workspace** — and for those,
  the quoted source line is **re-read from the local file** rather than trusted
  from the paste (workspace source is already in-channel; pasted "source" is
  not). Frames outside the workspace (site-packages, stdlib) keep only the file
  basename, line number, and function name; their source lines are dropped.
- **Exception message: redacted by default.** It becomes a value-free
  placeholder that preserves shape — `<redacted: 9 chars>` — unless it is
  provably value-free: it matches a fixed allowlist of known messages
  ("division by zero", "list index out of range", …), or every quoted token in
  it already appears verbatim in text the model has *already been shown this
  session* (the system context, the last live-schema refresh, the notebook
  source on disk). So `KeyError: 'revenue'` survives when `revenue` is a schema
  column — re-stating it reveals nothing new — while `KeyError: 'ACME Ltd'` is
  redacted.
- **Anything that doesn't parse is redacted, never passed through.** A line
  inside the detected block that matches no known shape becomes
  `<redacted line>`. Parser gaps fail closed.

The turn is then **held**, exactly like the PII guard's hold-and-confirm flow:
the analyst sees a preview of *precisely* what will be sent — the sanitised
traceback, with redactions visible — and a **Send sanitised** button. Unlike
the PII guard there is **no "send raw anyway" escape**: the raw paste is never
stored for forwarding, so no code path can transmit it. Prose around the
traceback is untouched (and still goes through the normal PII prompt scan).

From there the existing loop takes over: the copilot diagnoses using the
schemas + source it already has, and proposes a fix as a normal proposal card —
applied through `/api/ai/chat/apply` into `ai/cellwrite.py` with the usual
byte-level undo snapshot. The fix side of "traceback fixer" costs nothing new.

The guard is **on by default whenever the copilot is enabled** (it only ever
removes information, and the `Traceback (most recent call last):` anchor makes
false positives rare); a per-machine off switch exists for teams that accept
raw pasting, and turning it off is a weakening flip that requires an explicit
confirm on the Settings page.

Like `ai/pii.py` and `ai/secrets.py`, this is **defence in depth, not a
structural guarantee**: an analyst can still retype a redacted value in prose.
The docs must say so plainly.

## Architecture fit

Everything lands in L3 (`ai/`) and L4 (`hub/`), below and beside the existing
egress machinery; the only touch below L3 is the new config key parsed in L1
(`ai_config.py`, plus its flat property on `AppConfig` in `config.py`) — no
L2/L0 module changes.

- **New: `src/mooring/ai/traceback.py`** (L3, sibling of `ai/pii.py` /
  `ai/secrets.py`). Pure stdlib (`re`, `pathlib`) — detection, parsing, and the
  fail-closed rewrite. No marimo, no `urllib`/`http`, so the
  `marimo-internals-isolated` contract in `.importlinter` holds untouched.
  (`ai/secrets.py` already coexists with stdlib `secrets` — `ai/chat.py`
  imports the stdlib one absolutely — so the module name is safe precedent.)
- **Touched: `src/mooring/ai/egress.py`.** One new gateway function,
  `sanitize_traceback(...)`, wrapping the new module — the same pattern as
  `scrub_columns` delegating to `pii.scrub_columns`. The pinned rule in
  `tests/test_egress.py` (`test_no_module_bypasses_the_egress_scrub`) is
  extended: nothing outside `egress.py` imports `mooring.ai.traceback`
  directly, so a bypass is a review-visible change to one file.
- **Touched: `src/mooring/ai/chat.py`.** The shared outbound valve is
  `ChatBroadcaster._pii_gate` — used by both `StubChatSession` (`ai/chat.py`)
  and `CopilotChatSession` (`ai/session.py`), so one change covers every
  session class. The gate sanitises first, then runs `egress.guard_prompt` on
  the *sanitised* text. A held traceback turn stores the **sanitised** text in
  the existing `_pending` token map (the PII hold stores raw for "Send anyway";
  this hold never does), so the existing confirm plumbing —
  `send_confirmed` → `_pii_take` — forwards only sanitised text. A new
  `configure_traceback_guard(workspace=...)` mirrors `configure_pii` so the
  sanitiser can resolve frame paths; the hub passes the workspace it already
  holds per session.
- **Touched: `src/mooring/hub/server.py`.** `api_chat_open` (or
  `_make_chat_session`) arms the guard with the session's workspace — it
  already tracks `(workspace, notebook_rel)` per sid in `_chat_targets`.
  `api_chat_send` needs **no wire change**: the existing `confirm_token` path
  already calls `session.send_confirmed`, which now forwards sanitised text.
- **New SSE event kind** `"traceback"` on the existing stream
  (`/api/ai/chat/stream/{sid}` via `_sse_gen`), carrying the sanitised preview
  text, value-free redaction counts, and the one-time token.
- **Touched: `src/mooring/hub/static/chat.js` + `chat_core.js`.** A preview
  card alongside `addPiiHold` (which already posts
  `{sid, confirm_token: token}` back to `/api/ai/chat/send`); a pure
  `ChatCore` helper formats the preview so it is testable under `node --test`.
- **Config.** A new `[ai] traceback_guard` key (default `true`): parsed in
  `load_ai_config` (`src/mooring/ai_config.py`), exposed as a flat
  `ai_traceback_guard` property on `AppConfig`, registered as a `SettingSpec`
  in `src/mooring/hub/settings_schema.py` with a weakening-confirm like
  `ai.pii.enabled`.
- **New CLI (optional): `mooring ai traceback check`** in `src/mooring/cli.py`,
  modelled on `cmd_ai_pii_check` — reads a traceback from a file or stdin and
  prints the sanitised form, offline, so a security reviewer can see exactly
  what the rewrite does before trusting it.

Import direction: `hub → ai.chat → ai.egress → ai.traceback` — all downward;
`ai/` still never imports `hub`/`cli`; the L1 config-key addition imports
nothing new, and nothing in L2 or L0 is touched.

## Implementation plan

1. **Sanitiser core (M).** New `src/mooring/ai/traceback.py`: `detect(text)`,
   `sanitize(text, *, workspace, known_tokens) -> (text, findings)` with
   value-free findings (`(line, kind)`, like `pii.Finding`). Build the
   fail-closed parser against a corpus: plain, chained, `SyntaxError` (caret
   lines), Windows paths (`C:\Users\…`, backslashes, drive-letter case),
   non-ASCII messages, marimo-wrapped cell frames. Include the fixed
   value-free message allowlist. Add the `egress.sanitize_traceback` gateway
   and extend the `tests/test_egress.py` one-gateway rule. New
   `tests/test_traceback.py`. Independently shippable (pure functions, no UI).
2. **Chat valve + hub + UI (M).** Wire the gate into
   `ChatBroadcaster._pii_gate` in `src/mooring/ai/chat.py` (sanitise → hold →
   PII-scan the sanitised text); add `configure_traceback_guard`; arm it from
   `api_chat_open` in `src/mooring/hub/server.py`; broadcast the `"traceback"`
   event; render the preview card in `chat.js` with a `chat_core.js` helper;
   add the `[ai] traceback_guard` config key + `SettingSpec`. Update
   `docs/admins/ai-privacy.md` with a new best-effort section (see Testing).
3. **In-context token allowlist (S).** Feed `sanitize` the identifiers already
   in-channel: column names from the dataset schema and the session's
   `_last_live_schema` (seeded by `set_initial_live_schema`, refreshed per
   turn), plus names read from the notebook source on disk. This is what keeps
   `KeyError: 'revenue'` legible and rescues `NameError: name 'df2' is not
   defined`. Shippable after phase 2; until then messages are simply redacted
   more often.
4. **Offline preview CLI (S).** `mooring ai traceback check` beside
   `mooring ai pii check` in `src/mooring/cli.py` — no network, prints the
   sanitised rewrite and the redaction findings.

## Testing

All offline; GitHub is mocked with `responses` where the hub is involved.

- **New `tests/test_traceback.py`** — the parser corpus above, plus the two
  pinned invariants: (a) a `SECRET_VALUE_DO_NOT_LEAK` fixture planted in an
  exception message, a pasted source line, and a non-workspace frame path never
  survives `sanitize`; (b) fail-closed — arbitrary junk lines inside a detected
  block never appear in the output verbatim.
- **Extend `tests/test_egress.py`** — the one-gateway rule covers the new
  module (nothing outside `egress.py` imports `mooring.ai.traceback`), mirroring
  `test_no_module_bypasses_the_egress_scrub`.
- **Extend `tests/test_chat_session.py`** — a `StubChatSession` given a
  traceback-bearing prompt holds the turn; the broadcast event carries only the
  sanitised preview; `send_confirmed` with the token forwards sanitised text
  (`last_sent` proves it); no sequence of calls forwards the raw paste; the PII
  guard still fires on PII in the surrounding prose.
- **Extend `tests/test_hub.py`** — end-to-end over `/api/ai/chat/send`:
  hold → confirm token → forwarded text is sanitised; guard off ⇒ passthrough;
  the settings flip requires the 409 confirm.
- **JS** — a `ChatCore` preview-formatting helper tested in
  `tests/js/chat_core.test.js` (`node --test tests/js/`).

## Risks and mitigations

- **Over-redaction leaves the model unable to diagnose.** The commonest errors
  name a column or variable, and the live schema (`ai/introspect.py`) plus the
  phase-3 in-context allowlist usually preserve or let the model infer the
  missing key. The preview makes the loss visible, and the placeholder keeps
  type + length (`<redacted: str, 9 chars>`) as a diagnostic hint.
- **Parsing arbitrary tracebacks is finicky** — chained exceptions, Windows
  paths, non-ASCII, exception-group formats. The design accepts parser gaps and
  makes them fail closed (unparsed → redacted); the corpus tests pin that, and
  CI runs on Windows so path handling is exercised for real.
- **Users retype the redacted value in prose.** The existing PII guard is only
  a partial backstop — it is opt-in and catches structured kinds only, never a
  company name. Honest answer: this feature narrows the channel, it cannot
  close it; the preview copy and `docs/admins/ai-privacy.md` must say so.
- **No raw-send escape may frustrate.** Deliberate: an escape hatch on the
  confirm would recreate today's leak one click deep. The admin-level
  `[ai] traceback_guard = false` off switch (confirm-gated) is the only escape,
  and it is a policy decision, not a heat-of-the-moment one.
- **Privacy-claim honesty.** `ai-privacy.md` currently lists tracebacks as
  something the model never receives; once this ships, sanitised tracebacks
  *are* an egress. The page needs a new section (mirroring the structured-PII
  one) stating exactly what survives sanitising and that this channel is
  best-effort, not structural — shipping the feature without that update would
  make the spec dishonest.
- **False-positive detection on traceback-shaped prose** (log excerpts). Only
  the detected block is rewritten, surrounding text is untouched, and the
  preview shows the result — worst case is a needless confirm click.

## Dependencies and sequencing

- **Builds on the egress choke point** (`ai/egress.py` + the
  `tests/test_egress.py` one-gateway rule), which is already shipped — no
  roadmap prerequisite. Phases 1–2 can start immediately and ship
  independently of every sync-side page.
- **Copilot-extra-only**, like the [handover explainer](handover-explainer.md):
  both live entirely in `ai/` + the chat UI and are dormant without
  `mooring[copilot]`. Neither touches the sync engine, so they do not compete
  with [push guard](push-guard.md) / [pull digest](pull-digest.md) sequencing.
- The proposal→apply→undo path it rides on is shipped
  (`ai/cellwrite.py`, `notebook_undo`); no dependency on
  [review my changes](review-my-changes.md) or its cell-differ.
- See [architecture](../index.md) for the layer map and
  [ai-privacy](../../admins/ai-privacy.md) for the guarantee this feature must
  keep honest.
