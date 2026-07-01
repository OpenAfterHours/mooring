---
icon: lucide/newspaper
---

# Pull digest: what changed while you were away

!!! note "Status: proposed"
    Designed 2026-07 from a multi-agent ideation review; not yet implemented.
    Scope and endpoint names may change as the shared pieces from
    [Review my changes](review-my-changes.md) land.

## Problem

Pull is a black box. An analyst clicks **Pull**, the log card prints
`pulled reports/monthly.py`, and files on disk silently become something else.
There is no *who*, no *why*, and no sense of whether the change matters for the
report due today. The analyst is a non-developer who will never open a git log
— and mooring's whole promise is that they never have to.

GitHub's own UI can't answer the question either, because it doesn't know the
analyst's personal sync horizon. Mooring does: `manifest.py` records the
last-synced blob SHA per file and the branch head at the last clean sync
(`Manifest.head_commit`), so mooring can compute "everything that changed on
the branch **since you last looked**" — a per-person answer no shared commit
list gives.

The gap bites hardest after time away: come back from a week's leave, pull,
and fifteen files update at once. Which teammate touched the notebook you
co-own? Did anyone change the cell that computes the number you publish?
Today the only answer is to open each notebook and squint.

## Design

The core is deterministic and needs no AI.

**What it computes.** A digest is a list of per-file entries covering every
synced file that changed on `cfg.branch` between the analyst's horizon
(`Manifest.head_commit`) and the current branch head. Each entry carries: the
path, its current sync state (`remote changed`, `new remote`,
`deleted remotely`, or `conflict`), the author(s), a relative time ("2 days
ago"), and the commit message(s). Because mooring's Contents-API writes are
**one commit per file** (`GitHubClient.put_file`), a teammate's eight-file push
arrives as eight commits with near-identical messages — the digest groups
consecutive commits by author + message into one human-shaped "push" entry.

**Where it shows.**

- **Hub:** a "What's new" panel on the index page, populated on demand from a
  new header button — a separate endpoint kept off the `/api/state` hot path,
  like the adopt banner's `/api/discover` (which the hub calls once per
  repo-session, never on every refresh) — and automatically after a Pull: the
  pull response
  itself carries the digest of what just landed. Expanding an entry lazily
  fetches a cell-aware summary — "2 cells changed, 1 added" — computed locally
  by diffing the base and remote blobs with the shared cell differ introduced
  by [Review my changes](review-my-changes.md); non-notebook files fall back
  to plain line counts.
- **CLI:** a new `mooring whatsnew` subcommand printing the same entries.

**Watching.** A per-file "Watch" toggle (stored client-side in
`localStorage`, like the theme mirror) promotes a watched file's changes: its
row gets a highlight badge and its digest entry sorts first. The hub has no
background polling loop — `refresh()` runs on load and after actions — so
"while the hub is open" means "on every refresh", not a live ticker. That
keeps the hub simple and adds zero new requests.

**With the copilot installed** (the `mooring[copilot]` extra, like the
[handover explainer](handover-explainer.md)):

- An **Explain** button per entry produces an on-demand plain-English digest
  ("Sarah changed the revenue aggregation from monthly to weekly buckets").
  Both versions of the notebook **source** — already the sanctioned egress
  channel — go to the model through `ai/egress.py`'s scrubbers, and the result
  is cached against the `(base_sha, remote_sha)` pair so re-clicking is free.
  Explain never runs during pull and never blocks it.
- A deterministic **"may affect your work"** flag: names defined or columns
  referenced in the changed cells, cross-referenced against the names the
  reader's own local notebooks use. Pure local computation, no AI call.

**Conflicts stay loud.** A conflicted file appears in the digest with its
conflict state and links to the existing resolution flow
([resolving conflicts](../../users/conflicts.md)); the digest is read-only and
never resolves anything.

## Architecture fit

See the [architecture overview](../index.md) and `.importlinter` for the layer
rules. Everything here points down:

- **L1 — `src/mooring/github.py` (extended):** `GitHubClient` grows two read
  wrappers — a new `compare(base, head)` over
  `GET /repos/{owner}/{repo}/compare/{base}...{head}` (one call returns the
  commits and changed files for the whole horizon window) and a new
  `list_commits(path=None, branch=..., per_page=...)` over
  `GET /repos/{owner}/{repo}/commits` (the per-file fallback). Both are added
  to `GitHubClientProtocol` so the in-memory test fake keeps standing in.
- **L2 — new module `src/mooring/whatsnew.py`:** the digest core. Imports
  `github` (L1), `manifest`, and `sync` (lateral L2, for `StatusReport`,
  `PULL_STATES`, `is_synced_path`, `within_folders`) plus the new cell-differ
  module shared with [Review my changes](review-my-changes.md). It is added to
  the `sync-domain-is-core` contract's `source_modules` in `.importlinter`, so
  it can never import `ai/`, `editor`, or the adapters — which also makes
  "pull can never block on AI" structural, not aspirational.
- **L3 — new module `src/mooring/ai/digest.py`** (phase 4 only): builds the
  explain prompt from fragments passed through `egress.scrub_text` and calls
  the provider. It needs neither marimo nor raw HTTP, so the
  `marimo-internals-isolated` contract is untouched.
- **L4 — adapters:** new hub routes in `src/mooring/hub/server.py`, panel
  rendering in `src/mooring/hub/static/app.js` (pure helpers in a new static
  module, following the `files_tree.js` precedent), and the `whatsnew`
  subcommand in `src/mooring/cli.py`. The hub orchestrating L2 `whatsnew` and
  L3 `ai/digest` side by side is exactly what L4 is for.

One code-over-sketch adaptation: the ideation sketch led with
`list_commits(path, since)`, but the manifest's `head_commit` anchor makes the
compare API the right primary primitive (one request for the whole window).
`list_commits` remains as the mandatory fallback, because `sync.pull`
deliberately **blanks** `head_commit` after a conflict-skipping pull and
`_finalize_push` blanks it after a stale-remote push — the anchor is often
legitimately absent.

## Implementation plan

**Phase 1 — deterministic commit digest (CLI-first). Size: M.**

1. Extend `GitHubClient` in `src/mooring/github.py` with `compare(base, head)`
   and `list_commits(...)`; map errors through the existing `_check` (a 404 on
   a GC'd/force-pushed anchor raises `NotFound`, which callers treat as
   "anchor lost"). Add both methods to `GitHubClientProtocol` and to
   `FakeClient` in `tests/conftest.py` (which already tracks per-branch heads;
   teach its `_advance` to also record a commit log for the fake to serve).
2. New `src/mooring/whatsnew.py`: `DigestEntry` / `Digest` dataclasses and
   `pending_digest(client, cfg, report=None)`. Anchor at
   `manifest.load(cfg.workspace()).head_commit`; on a valid anchor call
   `compare`, filter files through `sync.is_synced_path` +
   `sync.within_folders` (the same visibility both sync sides use), and group
   consecutive same-author/same-message commits. On a blank/lost anchor, fall
   back to one `list_commits(path=...)` call per file in
   `report.by_state(*sync.PULL_STATES, sync.FileState.CONFLICT)`.
3. Register `mooring.whatsnew` in `.importlinter`'s `sync-domain-is-core`
   contract.
4. CLI: add a `whatsnew` parser in `_build_parser` and a branch in
   `_dispatch` (`src/mooring/cli.py`) calling a new `cmd_whatsnew` modelled on
   `cmd_status`.

**Phase 2 — hub panel, post-pull digest, cell summaries. Size: M.**

1. Add `Route("/api/whatsnew", hub.api_whatsnew)` to the routes list in
   `src/mooring/hub/server.py`, next to `/api/discover` (same on-demand
   philosophy). The handler wraps `whatsnew.pending_digest` with the
   `_sync_op`-style error mapping.
2. Extend `Hub._sync_op` with an optional `extra` dict merged into the
   response body, and have `api_pull` compute the digest **before**
   `sync.pull` runs (pull rewrites the manifest, destroying the horizon) —
   best-effort, so a digest failure never fails the pull.
3. New POST `/api/whatsnew/detail` taking `{path}`: fetch the base and remote
   blobs via `client.get_blob`, run the shared cell differ, return the
   "2 cells changed, 1 added" summary; fall back to `difflib` line counts for
   non-`.py` files. Cache on the `Hub` instance keyed
   `(path, base_sha, remote_sha)`.
4. Frontend: a "What's new" card in `index.html`; rendering + relative-time /
   grouping helpers in a new `src/mooring/hub/static/whatsnew.js` (pure,
   node-testable), wired from `app.js`'s `refresh()` and the pull handler.
   If the cell differ hasn't landed yet, ship line counts only.

**Phase 3 — watch toggles and the affects-you flag. Size: S.**

1. `app.js`: a Watch/Unwatch entry in `fileActions` persisting a per-repo path
   set in `localStorage`; `buildFileRow` badges a watched row whose state is
   in `PULL_STATES`; the digest panel sorts watched entries first.
2. `whatsnew.py`: a deterministic `affects(entry, workspace)` that intersects
   identifiers/column-name strings from the changed cells with those used in
   the reader's other local notebooks (stdlib `ast`; value-free). Rendered as
   a soft "mentions names your notebooks use" tag, never a gate.

**Phase 4 — copilot explain (extra-only). Size: M.**

1. New `src/mooring/ai/digest.py`: `explain(base_source, head_source,
   notebook_rel, provider)`; every fragment passes `egress.scrub_text` before
   assembly, mirroring how `build_system_context` scrubs at the choke point.
2. New POST `/api/ai/whatsnew/explain` in `server.py`, gated on
   `app_cfg.ai_enabled` and the per-notebook opt-out
   (`workspace_config.is_ai_disabled`, the same gate `_disabled_block`
   enforces for chat); reuses the hub's cached `_provider_for()` provider;
   caches results against the SHA pair.
3. Update [docs/admins/ai-privacy.md](../../admins/ai-privacy.md): notebook
   source is the existing sanctioned channel, but "two revisions of it, on a
   new endpoint" is a new consumer and the spec must say so honestly.

## Testing

All offline; GitHub is mocked with the `responses` library for the real
client, and the in-memory `FakeClient` elsewhere.

- `tests/test_github.py`: wire-format tests for `compare` and `list_commits`
  (pagination params, 404 → `NotFound`, rate-limit mapping).
- New `tests/test_whatsnew.py` (against the extended `FakeClient`): digest
  from a valid anchor; empty digest when `head_commit` equals the branch head;
  the fallback path when `head_commit` is blank — including a pin that a
  conflict-skipping `sync.pull` leaves the manifest in exactly the state the
  fallback handles; same-author commit grouping; scope filtering matches
  `is_synced_path`; conflicted files appear marked, never resolved.
- `tests/test_hub.py`: `/api/whatsnew` response shape; the pull response
  carries a digest reflecting **pre**-pull state; detail caching; the explain
  endpoint returns 403 for an AI-disabled notebook and is absent without the
  copilot extra.
- Invariant pins (new `tests/test_ai_digest.py`): a
  `SECRET_VALUE_DO_NOT_LEAK`-style fixture plants a checksum-valid PII value
  in a changed cell and asserts the outbound explain prompt never contains it
  (`egress.scrub_text` dropped the line); and a pin that `sync.pull` completes
  with the AI provider monkeypatched to explode — pull can never touch AI.
- JS: new `tests/js/whatsnew.test.js` (`node --test`) for the pure helpers —
  relative-time formatting, commit grouping, watch-set round-trip.

## Risks and mitigations

- **Legibility depends on the push note shipping first.** Today
  `sync._push_candidate` commits with machine noise
  (`Update {path} via mooring`), so a digest built now would faithfully show
  garbage messages. The push note from
  [Review my changes](review-my-changes.md) must land first; until then the
  digest still shows author, time, and cell counts, which is already more
  than "pulled x.py".
- **One commit per file.** The Contents API write path means multi-file
  pushes read as commit spam; the author+message grouping is load-bearing,
  not cosmetic, and is pinned by tests.
- **Anchor gaps.** `head_commit` is blanked by design after conflict-skips
  and stale pushes, and a rebased/force-pushed branch 404s the compare call.
  Both degrade to the per-file `list_commits` fallback; if that also fails,
  the panel shows the plain state list with no attribution rather than
  erroring.
- **REST rate limits on big repos.** One compare call per digest, blobs
  fetched two-per-file only on expand, everything cached against SHA pairs,
  and the panel loads on demand — never on the `/api/state` refresh path. The
  compare API also caps its response (~250 commits / 300 files); a truncated
  window degrades to the unattributed listing.
- **AI digests must never block pull.** Enforced structurally: `whatsnew.py`
  sits in the `sync-domain-is-core` contract and cannot import `ai/`; the
  explain endpoint is separate and on demand.
- **Wrong "affects you" flags either alarm or lull.** The flag is
  deterministic name-intersection only, worded softly, and gates nothing. If
  real use shows it over-triggering on common names (`df`, `date`), narrow it
  to column-name strings before making it more prominent.
- **Cell parsing fails on odd files.** The cell differ falls back to line
  counts for anything it can't parse; non-notebook files (data, PBIP members)
  always get line counts.

## Dependencies and sequencing

- **After [Review my changes](review-my-changes.md)** — it ships the push
  note (which makes commit messages legible) and the shared cell-differ
  module this page's phase 2 consumes. Phase 1 here has no hard dependency
  and could ship first, at reduced usefulness.
- **[Push guard](push-guard.md)** owns "recall last push"; its record of what
  *you* sent complements this page's record of what *they* sent.
- **[Staleness guard](staleness-guard.md)** shares the horizon arithmetic
  (`Manifest.head_commit` vs the live branch head) — the anchor helper in
  `whatsnew.py` should be written for both to reuse.
- **[Version history](version-history.md)** is the per-file deep dive; each
  digest entry links to it for "show me every version".
- **[Offline mode](offline-mode.md)**: the digest is network-only and must
  hide gracefully when offline, like the other sync controls.
- **[Handover explainer](handover-explainer.md)** and the
  [traceback fixer](traceback-fixer.md) are the sibling copilot-extra
  features; phase 4 follows their shared pattern — on-demand, routed through
  the egress choke point, cached, and documented in
  [ai-privacy.md](../../admins/ai-privacy.md).
