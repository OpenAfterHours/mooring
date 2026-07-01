---
icon: lucide/map
---

# Architecture plan (July 2026)

This is the agreed plan for mooring's next round of structural work. It comes out
of a full architecture review run on 2026-07-01 (subsystem mapping, a
blank-slate design panel, and adversarial critique, with every load-bearing
claim verified against the code). It supersedes the June 2026 review's
conclusion that thinning the hub was optional.

If you are new to the codebase, read [Architecture](index.md) first — this page
assumes you know the layering and the hard invariants.

## Verdict in one paragraph

The flat `src/mooring/` namespace **stays**, and a full ports/domain/adapters
repackage is **rejected (again)** — the import-linter contracts already enforce
the dependency direction a package tree would encode, and moving byte-stable,
well-tested files re-encodes that for zero runtime gain. What the codebase
genuinely lacks is a home for *application orchestration and live state* below
the two adapters. That gap is why `hub/server.py` grew from 836 to 2425 lines
(one `Hub` class: 46 routes, 6 locks, the chat registry, and the batch
orchestrator's runs as raw dicts), and why `cli.py` and the hub duplicate the
open/adopt/client flows verbatim — the `hub must not import cli` rule is right,
but it forces duplication when there is no shared layer underneath. The fix is
**exactly one new package, `app/`**, that both adapters render over.

## Target (the deltas only)

```
src/mooring/app/            NEW — application services + live state; imports NO adapter
├─ chat_service.py          chat session lifecycle; the SOLE egress.build_system_context caller
├─ batch_service.py         typed batch-run registry around ai.batch.BatchPlanner; real cancel()
├─ apply.py                 the SHARED per-notebook apply/undo owner
│                           (chat Apply, batch Apply, AND sync rollback all serialize here)
└─ notebooks.py             open_notebook / client_for / adopt_folders — the cli↔hub dedup

src/mooring/hub/
├─ server.py                hollowed IN PLACE: the Hub class stays (it becomes the app
│                           context/state-holder); handlers move out router-by-router
├─ sse.py                   ONE event_stream helper (collapses the chat + batch SSE generators)
├─ pages.py                 page rendering
├─ routes/                  setup · sync · files · settings · chat · batch
│                           each handler = parse → app call → serialise
└─ static/core.js           (opportunistic) shared ESM: fetch wrapper, applyTheme,
                            escapeHtml, SSE client — no bundler, no framework
```

Everything else — the sync core, `github.py`, the config resolution core,
`auth.py`, the `ai/` privacy machinery, `BatchPlanner` — is healthy and carries
over **verbatim**. Do not churn it.

!!! warning "Hard invariants every phase must preserve"

    - **Value-blindness**: the model sees schema and notebook source only, and
      everything it sees passes through `ai/egress.py`. See
      [why the copilot can't see your data](../admins/ai-privacy.md).
    - **Sync stays git-free and byte-stable**: no phase touches
      `sync.py` / `manifest.py` / `gitsha.py` decision logic.
    - **Frozen build stays lean**: no new runtime deps; optional extras
      (Copilot SDK, spaCy) must remain function-locally imported.
    - All six import-linter contracts stay green in every PR.

## The phases

Each phase is one small, revertible PR (P2 may be several), gated by the full
suite: `ruff` → `lint-imports` → `pytest` → `node --test tests/js/`. P0, P6 and
P7 are independent of the rest and land first; P1→P5 build on each other.

### P0 — Guardrails first, plus a real bug fix

Characterization tests that pin what the refactor must preserve, **before**
anything moves:

- an exhaustive route-table test enumerating all 46 routes by path + method +
  name (none exists today — a mechanical split could silently drop a route and
  pass CI);
- a **deterministic** apply-lock guard: assert that chat Apply, batch Apply,
  and sync rollback all acquire the *same* lock object (`_apply_lock` has three
  users, not two — do not write a timing-based concurrency test; they flake on
  Windows CI);
- a double-apply-of-one-proposal test pinning applied-once semantics;
- a `settings_schema` ↔ `AiConfig` key-consistency test.

Also ships the one user-visible bug the review found: `static/batch.js` is
missing `applyTheme` (present in `app.js`, `chat.js`, `settings.js`), so the
batch page never live-rethemes. ~3 lines.

### P6 — Egress mint gateway *(independent — can land right after P0)*

Make "nothing reaches the model except through egress" structural:

- `egress.to_tool_result(text)` and `egress.to_error_result(msg)` become the
  **only** constructors of the SDK's `ToolResult`. The SDK import must be
  function-local (mirroring `tools.py`) so `import mooring.ai.egress` keeps
  working without the `copilot` extra — add a test for that.
- Route every `ai/tools.py` return through them, including the two channels
  that are unscrubbed today: the three **data-dictionary tools**
  (`list_tables` / `describe_table` / `search_dictionary` return rendered
  dictionary text directly) and the **error channel** (`_err` ships raw
  exception strings). Honest framing: the dictionary is opt-in, secret-scanned
  team metadata, so this is defense-in-depth that forces *future* tools through
  egress — not a live data-value leak.
- Do **not** change existing scrub semantics (`get_schema` keeps its column
  scrub; the PII-off path is not silently re-scrubbed).
- New import-linter contracts: only `ai.egress` imports `copilot.tools`; the
  domain core and config never import the privacy cluster; the adapters call
  egress, never raw `scrub_columns`/`scrub_text`. The existing source-grepping
  test in `test_egress.py` stays as belt-and-suspenders.

### P7 — Lean-runtime as a lint fact *(independent, nearly free)*

Forbidden-external contracts (`allow_indirect_imports = False`): the domain
core and config layer have **no import path** to `marimo`, the Copilot SDK, or
spaCy; and spaCy is imported only in `ai/ner_spacy.py`. Deliberately **not**
`requests` — `sync → github → requests` is the legitimate REST path. And do not
assert "SDK confined to `ai/copilot.py`" — it is false today (`session.py`,
`server.py` also import it); that's a possible future tightening, not a freebie.

### P1 — Birth `app/`: the cli↔hub dedup

`app/notebooks.py` gets the near-verbatim duplicated flows: `open_notebook`
(from `hub._open` / `cli.cmd_open`), `client_for`, `adopt_folders`, and the
shared shadow policy. One critical semantic: **`client_for` raises** (e.g.
`AuthFailed`) — it must never `sys.exit`, or it would kill the hub process. The
CLI catches and translates to `sys.exit` with the existing guidance text; the
hub keeps raising. New contract: `mooring.app` must not import `mooring.hub` or
`mooring.cli` (and lower layers must not import `mooring.app`).

Upcoming features on the [roadmap](roadmap/index.md) want this seam: a
staleness guard at Open belongs in `open_notebook`, and a push guard belongs in
shared push orchestration — build them here once instead of twice.

### P2 — Hollow out the hub, rename **last**

Do **not** delete the `Hub` class or rename `server.py`: `test_hub.py`
(2083 lines, 12 `Hub(...)` sites), `test_settings.py`, `cli.py`, and the
frozen-build file glob all reference them by name. Instead, hollow it out in
place — `Hub` keeps its name and becomes the state-holder/app-context, while
the 46 handlers move to `hub/routes/*` as functions over it, and the two
near-identical SSE generators collapse into `hub/sse.py`. Stage it
router-by-router; hold a short freeze on new `server.py` feature work while it
lands (it's the hottest file in the repo). Any rename is a trivial final PR —
or never happens; names are cheap, churn isn't.

### P3 — `app/chat_service.py` + `app/apply.py`

Move the chat application service out of the web adapter: context assembly
(including the sole `build_system_context` call), session lifecycle, the
live-schema pipeline. `app/apply.py` takes `_apply_with_undo` plus the single
per-notebook apply/undo lock — and it must also serve the **sync rollback**
path (the third lock user), not just chat and batch. Re-point the
`SECRET_VALUE_DO_NOT_LEAK` tests at the service.

### P4 — `app/batch_service.py` *(after P3 — not parallel)*

Batch depends on chat's context builder and on `app/apply.py`, so it follows
P3. Wrap `ai.batch.BatchPlanner` (which stays pure and verbatim — it is the
model of what an `ai/` coordinator should look like); own run state,
apply/refine/force, reap/abort; add a first-class `/api/ai/batch/cancel`;
replace the private `broadcaster._broadcast` reach-through with a public
`ChatBroadcaster.emit_job()`.

### P5 — Typed `BatchRun` + ordered teardown *(and nothing more)*

The raw `dict[str, dict]` batch registry becomes a typed `BatchRun` with
`mark_applied(pid)` / `cancel()` / `is_reapable()` as methods — the
applied-once invariant gets one owner under one lock. The hub's `reload()` /
shutdown becomes an **ordered** teardown (chats → batches → provider →
editors), preserving the Windows process-group kill byte-for-byte, so a repo
switch can never half-tear-down or silently drop un-reviewed batch proposals.
**Deliberately out of scope:** re-granularizing the remaining locks. The coarse
locks work; splitting them is risk without user value. Keep the cross-cutting
config/lifecycle lock and the shared apply lock as they are.

## Execution order

```
P0 → P6 → P7 → P1 → (staleness-guard feature on the new seam)
   → P2 (short server.py feature freeze)
   → P3 → P4 → (push-guard feature on the new seam) → P5 → stop
```

Interleaving features onto the seams they need is intentional: each feature
validates the seam it ships on, and the refactor never becomes a multi-month
feature drought.

**P0–P7 is the stop line.** Beyond it, only opportunistic work when already in
those files: splitting `marimo_rt.py` by I/O profile, a shared
`static/core.js` ES module (single-owning the `escapeHtml` XSS contract),
unifying the two opposite-truthiness `_as_bool` coercers, and single-sourcing
the settings schema against `AiConfig`. Deleting the ~25 flat `ai_*`
forwarding properties is deferred — it's codebase-wide caller churn, not an
in-file cleanup.

## Rejected — and why (don't relitigate without new evidence)

- **Ports/domain/adapters repackage, DI container, services framework** — the
  import-linter already enforces the direction; moving ~120 tested files is
  ceremony a small team pays for forever.
- **An opaque `Outbound` wire type minted only by egress** — seriously
  considered, then dropped: the guarantee evaporates at the SDK's own public
  `str` field, and a module-level SDK import would break the base install. The
  P6 sole-constructor + lint-contract design captures ~the same guarantee at a
  tenth of the friction.
- **A contract that only egress may import `pii`/`ner`** — unsatisfiable: the
  privacy cluster is imported across `chat`/`context`/`scan`/`batch`/`copilot`
  and by both adapters, and its modules import each other.
- **Forbidding `requests` from the core** — `sync → github → requests` is the
  product's legitimate HTTP path.
- **Renaming `ai/` → `copilot/`, a frontend framework or bundler, an external
  session store, a big-bang rewrite** — churn or machinery without benefit at
  this size.

## Status

All phases landed on `feat/arch-migration` (2026-07-02), one commit per phase,
each gated by the full suite (`ruff → lint-imports → pytest → node`):

- [x] P0 — guardrail tests + `batch.js` theme fix *(two planned pins already
  existed: batch-apply idempotence and the settings↔config round-trip)*
- [x] P6 — egress mint gateway *(all 12 `ToolResult` sites route through
  `egress.to_tool_result`/`to_error_result`; dictionary tools + the error
  channel scrubbed)*
- [x] P7 — lean-runtime lint contracts *(8 → 9 import-linter contracts)*
- [x] P1 — `app/notebooks.py` dedup *(+ `client_for` raises, never exits)*
- [x] P2 — hollow out the hub *(server.py 2425 → ~900 lines; `Hub` and
  `server.py` keep their names as designed — rename-last held)*
- [x] P3 — `app/chat_service.py` + `app/apply.py` *(the sole
  `build_system_context` caller now lives in `app/`; the apply guard owns
  the lock all three write paths share)*
- [x] P4 — `app/batch_service.py` *(+ `POST /api/ai/batch/cancel`,
  `ChatBroadcaster.emit_job()` replaces the private reach-through)*
- [x] P5 — typed `BatchRun` + deterministic teardown-order pins *(lock
  re-granularization deliberately not done, as planned)*

Beyond the stop line, P8–P11 remain opportunistic — touch them only when
already in those files.

*Line numbers and counts in this page are as of `master` at v0.4.18
(2026-07-01); they will drift — trust the names.*
