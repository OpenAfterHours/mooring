---
icon: lucide/history
---

# Per-file version history and restore

!!! note "Status: proposed"
    Designed July 2026 from a multi-agent ideation review; not yet implemented.
    Scope may change — this page records the current plan and its reasoning.

## Problem

The shipped recovery tool — `mooring rollback` on the CLI, **Revert** in the hub's
Actions menu — only reaches one point in time: the last-synced checkpoint
(`sync.revert()` restores the manifest base blob). That covers "I broke it since
my last pull", and nothing else. The recovery scenarios analysts actually hit are
older and remote: *the numbers were right in last Tuesday's version* (but there
have been three pulls and two pushes since), or a teammate deleted a notebook,
the deletion propagated on the next pull, and now nobody on the team can get it
back without finding someone who knows git.

The bitter part is that the full history already exists. Every version of every
synced file sits in the team repo mooring itself pushed there — analysts just
have no door to it, because mooring is deliberately their only interface to the
repo and mooring never shows more than three SHAs per file. A git user would run
`git log -- file` and `git checkout <sha> -- file`; cloud platforms like Hex and
Deepnote ship exactly this feature. For mooring it is pure read-path work: the
data is on GitHub, the REST API exposes it, and no new write semantics are
needed — a restore is just a local file write that rides the existing three-way
sync.

## Design

A new **History…** item in the per-file Actions dropdown (the `fileActions` /
`actionsMenu` machinery in `src/mooring/hub/static/app.js`), shown for any row
that is tracked against the repo (not `LOCAL`-mode rows, not `new local` files —
a never-pushed file has no history). Clicking it opens a per-file history panel
listing versions newest-first: short SHA, author, date, and the commit message.
Today those messages are machine-generated (`sync.push()` defaults to
`"Update {path} via mooring"`), so the list is honest but terse; the push note
planned in [Review my changes](review-my-changes.md) is what makes it legible.
The list is lazily paginated — one page of 30 per request, with a "Show older"
button — because the commits API is the slow, page-at-a-time way to read
history and old repos have a lot of it.

Per version, three actions:

- **View** — the file's source at that commit, rendered read-only in the panel.
  Old versions are never opened in the marimo editor and never executed: they
  may not run under the repo's current dependencies, and executing unreviewed
  historical code on click would be a footgun.
- **Diff** — a unified diff of that version against the current local file,
  computed server-side with stdlib `difflib` and rendered read-only. When the
  cell-aware differ shared by [Review my changes](review-my-changes.md) and
  [Pull digest](pull-digest.md) exists, `.py` diffs upgrade to it.
- **Restore** — two explicit modes:
    - **Restore as copy** (default, always safe): writes the historic bytes to
      `{stem}.restored-{sha7}{suffix}` beside the file. The copy is a normal
      workspace file — it classifies `new local`, so it can be opened, compared,
      pushed, or deleted like anything else. (It deliberately does *not* use the
      `.remote-` marker from conflict resolution, which `sync.is_synced_path()`
      permanently hides from sync.)
    - **Restore over current**: snapshots the current bytes onto the local undo
      stack first (`notebook_undo.snapshot()`, exactly as `api_rollback` does,
      returning an `undo_token`), then overwrites the local file. The file then
      classifies `modified` — or `conflict` if the remote moved meanwhile — and
      flows through standard sync: pushed explicitly, conflict-checked by the
      Contents API SHA, never silent. The manifest is never touched, so the
      three-way detection stays honest.

The confirm dialog for restore-over must say the quiet part out loud: restoring
a version *older than your last pull* and then pushing intentionally replaces
newer team work that you already pulled. (A teammate's *concurrent* change is
still caught — the file shows `conflict` and push blocks — but work already in
your manifest base is yours to overwrite, and the diff preview plus the confirm
are what make that a decision instead of an accident.)

**Deleted files** get their own door: a workspace-level "Recently deleted"
listing (recent commits on the branch whose file list contains removals under
the synced scope), each restorable to its original path — where it classifies
`new local` and pushes back like any new file. This is the answer to "a
teammate's deletion propagated on pull".

CLI twins for scripting and support calls:

- `mooring history <path>` — print the version list (short SHA, date, author,
  message), with `--page` for older pages.
- `mooring restore <path> --at <sha> [--copy] [-y]` — restore that version,
  defaulting to over-current with the same non-interactive refusal idiom as
  `cmd_rollback` (refuse without `--yes` when stdin is not a TTY).

This explicitly **extends** the shipped revert, not duplicates it: revert stays
the one-click "discard my changes since the last sync", history is the time
machine. To keep the two from blurring, the hub's existing **Revert** menu item
is relabelled **Discard my changes** in the same change (a one-string edit in
`fileActions`), and the [conflicts guide](../../users/conflicts.md) is updated
to match.

## Architecture fit

Read `.importlinter` first; everything here points down.

- **L1 `src/mooring/github.py`** — two new methods on `GitHubClient`, plus the
  matching additions to `GitHubClientProtocol` (and its in-memory stand-in,
  `FakeClient` in `tests/conftest.py`):
    - a new `list_commits_for_path(path, branch, page)` wrapper over
      `GET /repos/{owner}/{repo}/commits?path=&sha=&per_page=&page=` — lazily
      paginated, going through `_check()` so `AuthFailed` / `RateLimited` /
      `NotFound` behave like every other call;
    - a new `get_file_at(path, ref)` returning `(blob_sha, bytes)` via
      `GET /repos/{owner}/{repo}/contents/{path}?ref=` — one request for the
      common case (the API inlines base64 content up to ~1 MB), falling back to
      the existing `get_blob(sha)` for larger files. This is deliberately
      cheaper than walking a full historic tree with `get_full_tree()`.
- **L2 `src/mooring/sync.py`** — two new functions beside `revert()`:
  `history(client, cfg, rel_path, page)` (shapes the commit list and decides
  which states have history at all) and
  `restore_version(client, cfg, rel_path, at, *, as_copy, snapshot_fn)`
  (fetches the bytes, writes locally via the existing `_write_blob()`, calls
  `snapshot_fn` before an overwrite exactly as `revert()` does, returns a
  `SyncResult`). No manifest writes, no `put_file` — restore is a pure local
  write. sync → github is an existing, allowed edge.
- **L4 `src/mooring/hub/server.py`** — new routes in the route table:
  `GET /api/history` (version list), `GET /api/history/file` (read-only source
  + diff), `POST /api/restore` (both modes; returns `undo_token` for
  overwrites), and later `GET /api/history/deleted`. They reuse the existing
  seams: `_ws_file()` for path validation, `_apply_lock` + `notebook_undo` for
  the snapshot (the same pattern as `api_rollback` / `api_undo`), `telemetry`
  events alongside the existing `rollback` / `undo` ones.
- **L4 `src/mooring/cli.py`** — `history` and `restore` subparsers in
  `_build_parser()`, `cmd_history` / `cmd_restore` using the existing
  `_client(cfg)` helper and the `cmd_rollback` confirmation idiom.
- **`src/mooring/hub/static/app.js`** — a History entry in `fileActions`, the
  panel UI, and reuse of the `recentlyReverted` token map so a restore-over
  gets the same one-shot **Undo** row action a revert gets today.

Nothing touches `ai/`: no history byte ever reaches a model, so there is no new
egress channel and [ai-privacy](../../admins/ai-privacy.md) needs no update.
No new modules, so no `.importlinter` change. GHE works unchanged — the commits
and contents endpoints resolve through `githost.api_root()` like every other
call, and the existing token scope already covers repo reads.

## Implementation plan

1. **Client + domain core** (M) — extend `GitHubClient` in
   `src/mooring/github.py` with `list_commits_for_path()` and `get_file_at()`;
   extend `GitHubClientProtocol`; add `sync.history()` and
   `sync.restore_version()` in `src/mooring/sync.py`. Teach `FakeClient` in
   `tests/conftest.py` to answer them — today `_advance()` keeps only the
   *current* tree per branch, so it must additionally record a per-branch list
   of `(head, tree snapshot)` pairs as commits happen. Shippable alone (no UI).
2. **CLI** (S) — `mooring history` and `mooring restore` in
   `src/mooring/cli.py` (`_build_parser()`, `cmd_history`, `cmd_restore`,
   dispatch in `_dispatch`). Gives support calls a tool before the hub UI
   exists.
3. **Hub** (M) — routes `api_history` / `api_history_file` / `api_restore` in
   `src/mooring/hub/server.py`; the history panel, the `fileActions` entry, and
   the **Revert → Discard my changes** relabel in
   `src/mooring/hub/static/app.js` (+ panel styles in `style.css`); user-docs
   touch-up in `docs/users/conflicts.md`.
4. **Recently deleted** (S–M) — `GET /api/history/deleted` walking the last N
   (capped, e.g. 30) commits via the commits list plus per-commit file lists
   (`GET /repos/{owner}/{repo}/commits/{sha}`, a new `get_commit()` beside
   `list_commits_for_path()`), filtered to removals under the synced scope via
   `sync.is_synced_path()` / `within_folders()`; a hub entry point outside the
   per-row menu (deleted files have no row); restore reuses `/api/restore`.
   On-demand only and cached per head commit — this is N+1 requests.
5. **Cell-aware diffs** (S, after [Review my changes](review-my-changes.md)) —
   swap the `difflib` rendering for the shared cell-differ for `.py` files.

## Testing

Offline throughout; GitHub is mocked with `responses` at the HTTP layer and
`FakeClient` above it.

- `tests/test_github.py` — `responses`-mocked tests for
  `list_commits_for_path()` (pagination params, empty page), `get_file_at()`
  (inline content, the >1 MB fall-through to `get_blob`), and that both raise
  `RateLimited` / `AuthFailed` through `_check()` like existing calls.
- New `tests/test_sync_restore.py` (sibling of the `revert` coverage in
  `tests/test_sync.py`) — restore-as-copy classifies `new local`; restore-over
  classifies `modified` and pushes cleanly when the remote is unmoved;
  restore-over with a moved remote classifies `conflict` and **push blocks**;
  `snapshot_fn` receives the pre-overwrite bytes; the invariant pins: restore
  performs **no `put_file`/`delete_file`** and **never mutates
  `manifest.json`** (assert against `FakeClient` and the manifest on disk).
- New `tests/test_cli_history.py`, mirroring `tests/test_cli_rollback.py` —
  history prints versions; restore refuses without `--yes` when
  non-interactive; `--copy` writes the sibling file; needs-login error path.
- `tests/test_hub.py` — the three routes: path-escape rejection via
  `_ws_file()`, the `undo_token` round-trip through `/api/undo` (including the
  409 when a later write sits on top of the stack), and a pin that
  `/api/history/file` is read-only — it must never touch `self.editors` or
  write to the workspace.
- `tests/js/` (`node --test`) — any pure helpers the panel needs (short-SHA and
  date formatting, version-row shaping) go in an importable module the way
  `chat_core.js` does, with tests beside `files_tree.test.js`.

## Risks and mitigations

- **Terminology collision with Revert.** Two restore-ish verbs in one menu
  confuse exactly the users this is for. Mitigation: the relabel to "Discard my
  changes", distinct confirm copy per action, and History always leading with
  *when* ("last Tuesday, by Maria") rather than SHAs.
- **Restoring past the manifest base, then pushing, overwrites newer team
  work.** By design — but it must be a decision. The diff preview, the explicit
  confirm wording, and defaulting the hub flow to *restore as copy* keep the
  destructive path deliberate. Concurrent remote changes are still caught as
  `conflict` by the existing three-way machinery and the Contents API SHA check.
- **The commits API is slow on old repos and does not follow renames.** History
  for a renamed file starts at the rename. Documented, not chased — rename
  tracking would mean walking trees commit-by-commit, a cost mooring's audience
  never asked for. Lazy pagination keeps the panel responsive.
- **Old versions may not run today.** The repo's `pyproject.toml` / `uv.lock`
  have their own history that a per-file restore deliberately does not touch.
  Mitigation: View is read-only, and the restore confirm notes that old code
  meets current dependencies.
- **Undo stack interplay.** A restore-over shares the per-notebook undo stack
  with AI Apply and Revert; the existing token discipline (`api_undo` refuses a
  superseded token with 409) already covers this — reuse it, don't fork it.
- **PBIP artifacts.** Restoring a single member corrupts the artifact, so the
  hub gates Restore the way Revert is gated today (`.py` rows, no PBIP
  members); the CLI warns instead of blocking.
- **Rate limits on "Recently deleted".** N+1 requests; on-demand, capped, and
  cached per head commit, with the standard `RateLimited` surface if it trips.

## Dependencies and sequencing

- Independent to ship: phases 1–3 need nothing from other plans and sit in the
  "history and legibility" step of the [roadmap ordering](index.md).
- [Review my changes](review-my-changes.md) — its push note is what turns this
  panel's commit messages from `Update x via mooring` into human history, and
  its cell-differ replaces the `difflib` rendering (phase 5). History is fully
  usable before either lands; it just reads better after.
- [Pull digest](pull-digest.md) — the other consumer of the shared cell-differ;
  no direct coupling to this plan.
- [Push guard](push-guard.md) — "recall last push" there is the *undo my push*
  complement to this plan's *bring back an old version*; and any restored bytes
  that head back to the repo pass through the guard like every push.
- [Local safety net](local-safety-net.md) — generalises the undo affordance
  this plan reuses (`notebook_undo` is `.py`-only today); when its trash lands,
  non-`.py` restore-over can become undoable too.
- [Duplicate as draft](duplicate-as-draft.md) — restore-as-copy is its
  historical cousin; the two should share copy-naming conventions.
- [Offline mode](offline-mode.md) — history is inherently online; the panel
  and menu entry grey out under its offline state rather than erroring.
