---
icon: lucide/clock
---

# Staleness guard at Open and a workspace freshness banner

!!! note "Status: proposed"
    Designed 2026-07 from a multi-agent ideation review; not yet implemented.
    Scope and details may change during implementation.

## Problem

The most common conflict in a mooring workspace is self-inflicted. An analyst
opens a notebook a teammate updated upstream since their last pull, edits the
stale copy for two hours, then hits a blocked push and a per-file resolve flow
they didn't see coming. Nothing warned them at the moment of choice — the
moment they clicked **Open**.

The information already exists. Every hub refresh calls `sync.status()` (via
`api_state` in `src/mooring/hub/server.py`), which does the live three-way
comparison, and each file row already carries its state — `remote changed`,
`conflict`, `deleted remotely` all mean "the team side moved since your last
sync". But that state is rendered as a small badge on the row, and **Open**
behaves identically whether the badge says `synced` or `remote changed`. The
guard is missing at the decision point, not in the engine.

There is a second, quieter gap: the hub only refreshes on page load, after an
action, or when the user clicks **Refresh** (`refresh()` in
`src/mooring/hub/static/app.js` — there is no polling loop). A hub tab left
open overnight shows yesterday's states, so even the badge can lie by the time
the user clicks.

## Design

Two user-visible pieces, both advisory — **Open is never blocked**.

**1. A confirm at Open time.** Clicking **Open** on a `.py` row whose state is
`remote changed`, `conflict`, or `deleted remotely` shows a small dialog
instead of opening immediately:

> A teammate updated `sales-analysis.py` after your last pull.
> Editing your copy now will end in a conflict at push time.
>
> **`Pull latest and open`** · `Open my copy anyway` · `Cancel`

- **Pull latest and open** runs the normal pull (`POST /api/pull`) and then
  opens the file. This is a whole-workspace pull, exactly like the header
  **Pull** button — `sync.pull()` in `src/mooring/sync.py` has no per-file
  variant and this feature does not add one. The existing log card
  (`showLog`) shows what was pulled; conflicted files are skipped by pull as
  today, never silently overwritten.
- **Open my copy anyway** opens the stale copy and records a per-session
  dismissal keyed by `path → remote_sha`, so the dialog does not nag a user
  who has already decided to diverge. It re-arms only if the remote blob
  moves *again* (a new `remote_sha` for that path).
- For a `conflict` row the primary button changes: pull skips conflicts, so
  the dialog instead points at the row's existing resolve actions (Use
  remote / Keep both / Push as copy) and offers only "Open anyway / Cancel".

The dialog is a native `<dialog>` with three explicit `<button>`s — the same
lesson as the row actions menu in `app.js` (`actionsMenu`): no control where a
stray keypress fires a destructive default. The safe action gets focus; "Open
my copy anyway" is never the default.

**2. A freshness banner and focus refresh.** A one-line banner in the files
card (alongside `#review-banner` / `#adopt-banner` in
`src/mooring/hub/static/index.html`):

> Last checked 3 hours ago — 2 teammate updates waiting. **Refresh**

The age is client-tracked: `refresh()` records a timestamp on each successful
`/api/state`. (Note for implementers: there is *no* stored "last refreshed"
time anywhere — `manifest.py` has no timestamp field — and none is needed,
because `/api/state` recomputes status live against GitHub on every refresh.
Freshness is a property of the open tab, not of the workspace.) The
"updates waiting" count comes from the rows already in the state payload.
A `visibilitychange`/`focus` listener triggers `refresh()` when the tab
regains focus and the last check is older than a throttle (60 s), so an idle
tab heals itself before the user clicks anything.

**Near-open freshness check.** Because the dialog decision is made from the
last-rendered state, a light check runs when Open is clicked: a new
`GET /api/freshness` endpoint compares a live
`GitHubClient.get_branch_head(cfg.branch)` (one fast REST call,
`src/mooring/github.py`) against the head of the report `api_state` last
rendered. If the head moved, the frontend awaits a full `refresh()` and
re-evaluates the row; if the call fails or exceeds a ~2 s timebox
(`AbortController`), Open proceeds silently — offline or rate-limited users
are never blocked or nagged.

**"Who and when" enrichment (optional, later phase).** The ideation sketch
assumed "Maria updated this 2 hours ago" was available; it is not. The Git
Data API reads (`get_full_tree`, `get_blob`) carry no authors or dates. It
needs a new `GitHubClient` method wrapping
`GET /repos/{owner}/{repo}/commits?sha=<branch>&path=<path>&per_page=1`, one
extra REST call made only when the dialog is about to show. The base design
works without it (generic wording), so it ships last.

**CLI: explicitly out of scope.** `cmd_open` in `src/mooring/cli.py` makes no
network calls today and stays that way — an offline `mooring open` must keep
working. `mooring status` already surfaces the same per-file states for
terminal users.

## Architecture fit

- **L4 (hub)** — nearly everything: `src/mooring/hub/server.py` (one new
  endpoint, a remembered last-head, one field added to the `/api/state` rows)
  and `src/mooring/hub/static/` (`app.js`, `index.html`, `style.css`, a new
  pure helper `freshness.js`). The hub already imports `sync` (L2) and
  `github` (L1); imports keep pointing down, so every `.importlinter`
  contract holds unchanged.
- **L1 (`github.py`)** — only in the optional enrichment phase: a new
  `last_commit_for_path()` method on `GitHubClient`. It is deliberately *not*
  added to `GitHubClientProtocol` — that protocol is the surface the sync
  core depends on, and sync doesn't need commit metadata.
- **Zero sync-engine changes.** `sync.py`, `manifest.py`, `gitsha.py` are
  untouched. The states surfaced (`FileState.REMOTE_CHANGED` etc.) and the
  per-file `remote_sha` already exist on `FileStatus`; the only change is
  exposing `remote_sha` in the row dicts `_files_artifacts` builds.
- **No AI surface.** Nothing here reaches `ai/` or any model; blob SHAs,
  commit dates, and GitHub logins are repo metadata, not data values, and
  they travel only hub → browser on `127.0.0.1`.

New things, explicitly: a `GET /api/freshness` route; an optional
`GET /api/file/last-change` route; a new static helper
`src/mooring/hub/static/freshness.js`; a new `GitHubClient.last_commit_for_path`
(phase 4 only). No new Python modules, commands, or config keys are required
(one optional settings knob is noted in phase 2).

## Implementation plan

**Phase 1 — Open-time confirm from cached state (S).** Independently
shippable; delivers most of the value.

1. In `src/mooring/hub/server.py`, extend the row dicts in
   `_files_artifacts` with `"remote_sha": f.remote_sha` when it is not
   `None` (mirroring the existing `github_url` gating).
2. Add `src/mooring/hub/static/freshness.js` — pure, DOM-free, exposed as a
   bare global the way `files_tree.js` exposes `FilesTree`: `warnState(file,
   dismissedMap)` (returns `"pull" | "conflict" | null`), plus the dismissal
   keying logic.
3. Add a `<dialog id="stale-dialog">` with three explicit buttons to
   `src/mooring/hub/static/index.html`; style in `style.css`.
4. In `app.js`, gate `openAction(path)` on the row looked up in `lastFiles`:
   consult `Freshness.warnState`, show the dialog, wire **Pull latest and
   open** to `action("/api/pull", {})` followed by the open, and record
   dismissals in a session `Map` (the `recentlyReverted` map is the idiom).
   PBIP artifact headers keep their current Open; extending the guard to
   artifacts (aggregate `to_pull` on the artifact dict) is a follow-up.

**Phase 2 — Freshness banner + focus refresh (S).**

1. In `app.js`, record `lastStateAt` on each successful `refresh()`; render a
   `#freshness-banner` div (added to `index.html` beside `#review-banner`)
   with the age and the pending-pull count; clicking it calls `refresh()`
   (same as `#btn-refresh`).
2. Add `ageText(ms)` and `shouldAutoRefresh(lastStateAt, now, throttleMs)` to
   `freshness.js`; wire a `visibilitychange` + `focus` listener that calls
   `refresh()` through the throttle.
3. Optional: a `ui.refresh_on_focus` toggle registered in
   `src/mooring/hub/settings_schema.py` (with a matching `AppConfig`
   accessor in `config.py`). Default is on; skip the knob entirely if the
   throttled behaviour proves uncontroversial.

**Phase 3 — Near-open head check (S).**

1. In `server.py`, have `api_state` remember the rendered report's
   `head_commit` per workspace (a small dict on `Hub`, keyed like
   `self.editors`); add an `api_freshness` endpoint that calls
   `self.client().get_branch_head(cfg.branch)` and returns
   `{"fresh": bool, "head": sha}`; on `GitHubError` return 502 like
   `api_discover` does. Register the route in the `create_app` route list.
2. In `app.js`, before the phase-1 dialog decision, `fetch("/api/freshness")`
   with an `AbortController` timeout; on `fresh: false` await `refresh()` and
   re-evaluate the row; on any error or timeout, proceed as if fresh.

**Phase 4 — who/when enrichment (M, optional).**

1. Add `GitHubClient.last_commit_for_path(branch, path)` in
   `src/mooring/github.py` (commits list API, `per_page=1`), returning author
   login and commit date or `None`.
2. Add a hub endpoint (`/api/file/last-change`) that calls it; `app.js`
   fetches it lazily while the dialog opens and upgrades the wording to
   "@maria updated this 2 hours ago" (reusing `ageText`), degrading to the
   generic copy on failure.

## Testing

- **`tests/test_hub.py`** (offline; the `configured` fixture injects the
  in-memory `FakeClient` from `tests/conftest.py`): rows carry `remote_sha`
  exactly when the remote blob exists; `/api/freshness` reports fresh after a
  state render and stale after moving the fake's branch head; a
  `GitHubError` from the fake maps to 502. The invariant pin: **`/api/open`
  has no new server-side gate** — a `remote changed` file still opens (the
  guard is purely client-side and advisory).
- **`tests/test_github.py`** (GitHub mocked with the `responses` library):
  `last_commit_for_path` request shape and parsing, including the empty
  result (file with no commits on the branch → `None`).
- **`tests/js/freshness.test.js`** (run via `node --test tests/js/`, like
  `files_tree.test.js`): `warnState` for every row state (only the three
  remote-moved states warn; `synced`, `modified`, `local` etc. never do);
  dismissal suppresses re-warn for the same `remote_sha` and re-arms on a new
  one; `shouldAutoRefresh` respects the throttle; `ageText` boundaries.
- Existing tests to keep green: the `/api/state` shape assertions in
  `tests/test_hub.py` (rows gain a key; nothing is removed) and
  `tests/js/files_tree.test.js` (untouched — grouping doesn't read
  `remote_sha`).

## Risks and mitigations

- **The cached state can be stale at the click moment.** Mitigated by the
  phase-3 head check; a TOCTOU window remains (a teammate pushes between the
  check and the open). Accepted: the push path's optimistic concurrency and
  conflict blocking stay the backstop — this feature moves most pain earlier,
  it doesn't claim to eliminate it.
- **It must never block Open.** Every network check is advisory and
  timeboxed; failure, offline, and rate-limit all fall through to opening.
  [Offline mode](offline-mode.md) tightens this further (the banner should
  say "offline" rather than an ever-growing age once that page lands).
- **Nagging users who chose to diverge.** Session dismissals keyed to the
  remote blob SHA re-prompt only when the remote moves again. If that still
  proves noisy for long-lived divergence, a per-file opt-out in the synced
  `mooring.toml` is the escape hatch — deferred until someone asks.
- **Focus auto-refresh vs. API rate limits.** A refresh costs 1–3 REST calls
  (head, plus commit + tree when the head moved). The 60 s throttle and the
  visibility gate keep an idle-all-day tab to a handful of calls.
- **Dialog fatigue / dangerous defaults.** Three explicit buttons, safe
  default focus, and the dialog appears only for the three remote-moved
  states — a clean workspace never sees it.
- **"Pull latest and open" pulls more than one file.** Same semantics as the
  header Pull; the log card lists every pulled file and conflicts are
  skipped, not overwritten. Making pull per-file would be an L2 change with
  its own manifest subtleties — deliberately avoided here.

## Dependencies and sequencing

Self-contained: no shared modules with other roadmap pages and no
prerequisites, so it can ship first among the sync-safety features.

- [Push guard](push-guard.md) is the complementary *push-time* seam (and owns
  "recall last push"); this page is the *open-time* prevention that makes the
  push guard fire less often.
- [Pull digest](pull-digest.md): once it exists, the "Pull latest and open"
  dialog is a natural place to preview *what* the pull will bring; today the
  dialog shows only the log lines after the fact.
- [Local safety net](local-safety-net.md) makes "Open my copy anyway" a safer
  choice — a deliberately diverging local edit gains a recovery path.
- Unrelated to the copilot pages ([handover explainer](handover-explainer.md),
  [traceback fixer](traceback-fixer.md)); nothing here needs the
  `mooring[copilot]` extra or touches
  [AI privacy](../../admins/ai-privacy.md).
