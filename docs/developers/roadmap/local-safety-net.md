---
icon: lucide/life-buoy
---

# Local safety net: trash, universal Undo, activity ledger

!!! note "Status: proposed"
    Designed July 2026 from a multi-agent ideation review; not yet implemented.
    Scope may change as the pieces land.

## Problem

Mooring's "nothing is silently lost" promise currently covers only the remote
side: the Contents API's `sha` parameter rejects stale writes, and the
three-way engine in `src/mooring/sync.py` never auto-resolves a conflict. The
**local** side has no equivalent. The hub offers several one-click actions that
destroy local bytes: the conflict row's **Use remote**
(`ConflictStrategy.THEIRS` via `sync.resolve`), **Delete**
(`deletion.delete`), a pull run with the `theirs` strategy, and **Revert**
(`sync.revert`). An adversarial review of the actions-dropdown work proved the
misclick is real — on Windows, a focused menu plus one stray keypress was
enough to fire "Use remote" and silently discard local edits, which is exactly
why the menu became a click-only `<details>` widget. The menu fix reduces the
odds; it does not make the outcome recoverable.

Recovery exists today only for one narrow case: a **Revert** of a modified
`.py` snapshots the pre-image via `notebook_undo.snapshot` and the hub shows a
one-shot per-row Undo (`recentlyReverted` in
`src/mooring/hub/static/app.js`, `/api/undo` in
`src/mooring/hub/server.py`). Everything else — a deleted notebook, a
conflict resolved the wrong way, an overwritten `.csv` — is gone, and the
analyst has no git reflog to fall back on because analysts have no git.

There is also no answer to "what just happened?". Sync results are shown once
in the summary line and then discarded, so a confused user (or the teammate
helping them) cannot reconstruct yesterday's pushes, pulls, and deletes.

## Design

Three parts, all strictly local to the workspace. Nothing here talks to
GitHub, and nothing here is ever synced: everything lives under the
workspace's `.mooring/` state directory, which `sync.is_synced_path` already
excludes structurally (a dot-directory), the same way
`.mooring/undo/` snapshots and `manifest.json` are excluded today.

**(a) Trash.** Every mooring-initiated code path that overwrites or removes
local bytes first deposits the pre-image into a new
`<workspace>/.mooring/trash/` store. Each deposit is one flat blob file plus a
small JSON index entry recording the original relative path, a token, the
action that caused it (`resolve-theirs`, `pull-theirs`, `delete`,
`pull-overwrite`, `revert`), a UTC timestamp, and the blob SHA the destructive
action wrote afterwards (computed with `gitsha.local_blob_sha`, used to detect
supersession at restore time). Deposits are skipped for byte-identical
overwrites and for files above a per-file cap. Retention is
N-per-file / 14 days / a total-size cap, pruned best-effort at hub start.

!!! warning "Adaptation from the sketch"
    The ideation sketch stored pre-images as
    `trash/<timestamp>/<rel-path>`, mirroring the directory tree. The code
    already solved this problem differently: `notebook_undo._key` flattens a
    rel-path into an injective `slug-hash8` name precisely because readable
    slugs collide across separators (`a/b.py` and `a_b.py` slug identically);
    flat names also avoid mirrored trees deepening paths toward Windows'
    `MAX_PATH`. The trash reuses that idiom — flat `slug-hash8-<token>` blob names, with the true
    rel-path carried in the JSON index.

**(b) Universal Undo.** Hub responses for destructive actions carry the trash
token(s), and the frontend shows a transient toast — *"Local copy replaced —
Undo"* — that calls a new token-addressed restore endpoint. This generalises
the existing `/api/rollback` → `undo_token` → `/api/undo` idiom: restore
refuses with a 409 when the file's current blob SHA no longer matches the SHA
recorded at deposit time (a later write is on top), mirroring how `api_undo`
refuses a superseded token today. A restore deposits the *current* bytes back
into the trash first, so Undo is itself undoable, and it never touches the
manifest — the three-way engine simply reclassifies the file (MODIFIED /
NEW_LOCAL / CONFLICT) on the next status, so a restore can never silently
diverge from the remote. A Trash panel in the hub and `mooring trash
list` / `mooring trash restore` on the CLI cover discovery after the toast is
gone.

**(c) Activity ledger.** A local append-only JSONL journal,
`<workspace>/.mooring/activity.jsonl`, written at the seams where the
adapters already call `telemetry.log_event`: pull/push/propose/adopt
(`_sync_op` in `src/mooring/hub/server.py`), delete (`api_delete`,
`cmd_delete`), revert/undo (`api_rollback`, `api_undo`, `cmd_rollback`), AI
apply/rollback (`api_chat_apply`, `api_chat_rollback`), and trash restores.
The hub renders it as human sentences with relative times — *"Yesterday
16:42 — you pushed sales_review.py"* — filterable per file, each entry
linking to its matching trash/undo token when one exists. It lives behind a
single header link (the `/settings` page idiom), not on the front page. The
ledger is **not telemetry**: the opt-in central log (`telemetry.py`) ships
event records — op names and counts, *never file paths* — to an admin sink;
the ledger holds filenames and stays on the machine, full stop. The docs must say so explicitly.

User-visible behaviour, end to end: an analyst misclicks **Use remote** on a
conflicted notebook. The overwrite happens (behaviour unchanged), but a toast
appears: *"Local copy replaced — Undo"*. One click puts their bytes back and
the row shows `conflict` again. Two weeks later, asked "did you push the June
report?", they open **Activity**, filter by the file, and read the answer.

## Architecture fit

- **New: `src/mooring/trash.py` and `src/mooring/activity.py`** — leaves in
  the spirit of `notebook_undo.py`, importing only `mooring.paths`
  (`safe_write_bytes`) and, for trash, `mooring.gitsha`. Both should be added
  to the `foundation-is-pure` source list in `.importlinter` (neither `paths`
  nor `gitsha` is a forbidden target of that contract, so the contract holds).
- **Touched, L2:** `sync.py` (deposit hooks in `_apply_remote_or_keep_both`,
  the pull overwrite/unlink arms, and `resolve`; a `trashed` field on
  `SyncResult`) and `deletion.py` (deposit before each `unlink` in `delete`).
  The `sync-domain-is-core` contract forbids sync/deletion importing
  `ai`/`editor`/`hub`/`cli` — importing a new L0 leaf is fine, and mirrors
  `sync.py`'s existing `gitsha` import. Note the deliberate divergence from
  `sync.revert`'s `snapshot_fn` injection: that callback exists to keep a
  *notebook_undo* dependency out of sync.py, but threading callbacks through
  `pull`, `resolve`, and `_apply_remote_or_keep_both` would be noisier than a
  direct leaf import, and the import direction is unambiguous.
- **Touched, L4:** `hub/server.py` (new routes, ledger writes beside the
  existing `telemetry.log_event` calls, prune at start), `hub/static/app.js`
  (+ a new `activity.html`/`activity.js` page), and `cli.py` (new
  subcommands, ledger writes). Ledger orchestration sits in the adapters, the
  same altitude as telemetry — the domain core stays ignorant of it.
- **New endpoints:** `GET /api/trash`, `POST /api/trash/restore`,
  `GET /api/activity`. **New CLI:** `mooring trash list`,
  `mooring trash restore`, `mooring activity`. **New config:** a `[trash]`
  section (`keep_days`, `keep_per_file`, `max_file_mb`, `max_total_mb`) in
  `config_default.toml`, loaded onto `AppConfig` in `config.py`.
- **Untouched:** `github.py`, `manifest.py`, `ai/*`. The AI never sees the
  trash or the ledger — neither is reachable through the copilot's tools, and
  no ledger text is ever added to model context (see
  [ai-privacy](../../admins/ai-privacy.md); no change needed there because no
  new egress channel exists).

## Implementation plan

**Phase 1 — trash core and capture (S).** Shippable alone: bytes are saved
even before any UI exists.

1. New `src/mooring/trash.py`: `deposit(workspace, rel_path, data, action,
   after_sha) -> token`, `entries(workspace)`, `restore(workspace, token)`,
   `prune(workspace, keep_days, keep_per_file, max_total_mb)`. Flat
   `slug-hash8-<token>` blob names (reuse the `notebook_undo._key` recipe),
   one `index.json` per deposit, writes via `paths.safe_write_bytes`.
2. Hook `src/mooring/sync.py`: in `_apply_remote_or_keep_both` (the THEIRS
   overwrite and unlink — this covers both `pull(strategy=THEIRS)` and the
   hub's Use remote via `resolve`), in `pull`'s `REMOTE_CHANGED` overwrite
   and `DELETED_REMOTE` unlink arms, and in `resolve`'s `PUSH_COPY` restore.
   Record `(rel_path, token)` pairs on a new `SyncResult.trashed` field.
   (Plain-pull overwrites destroy only bytes equal to the manifest base —
   recoverable from GitHub via `get_blob` — but trashing them too makes
   recovery one click and offline.)
3. Hook `src/mooring/deletion.py`: in `delete`, deposit each file's bytes
   before `target.unlink()`; return tokens alongside the removed paths.
4. Config: add the `[trash]` keys to `config_default.toml` and `AppConfig`
   in `src/mooring/config.py`. Sensible defaults: 14 days, 10 per file,
   per-file cap = `max_file_mb` (45, matching the existing `[sync]
   max_file_mb` — mooring refuses to push anything larger), 200 MB total.
5. Prune at hub start: a best-effort background call in `run_hub` in
   `src/mooring/hub/server.py`, next to `telemetry.log_event("hub_start")`.

**Phase 2 — Undo toast, Trash panel, CLI (M).**

1. Plumb `SyncResult.trashed` and the delete tokens into responses:
   `_sync_op`, `api_resolve`, `api_delete`, `api_pull` in
   `src/mooring/hub/server.py`.
2. Add `GET /api/trash` and `POST /api/trash/restore` routes in `create_app`;
   restore takes `{token}`, 409s on SHA supersession (mirror `api_undo`'s
   `_UNDO_SUPERSEDED` shape), deposits current bytes first, and is held under
   the same `_apply_lock` as `api_rollback` so it cannot race an AI Apply.
3. Frontend: a `showUndoToast(trashed)` helper in
   `src/mooring/hub/static/app.js`, invoked from `deleteAction`,
   `revertAction`, and the resolve handlers in `fileActions`; auto-dismiss,
   one Undo button per action. Keep the existing `recentlyReverted` /
   `/api/undo` path for `.py` reverts untouched — the two stores stay
   distinct (see Risks).
4. CLI: `trash` subparser in `_build_parser` in `src/mooring/cli.py`
   (`list`, `restore <token>`), following the `shadow`/`deps`
   sub-subparser idiom.

**Phase 3 — activity ledger and panel (S).**

1. New `src/mooring/activity.py`: `record(workspace, op, **fields)` appending
   one JSON line; `read(workspace, limit=200, path=None)`. Append-only; a
   size-triggered rotation keeps the file bounded.
2. Write entries beside every existing `telemetry.log_event` call in
   `hub/server.py` (`_sync_op`, `api_delete`, `api_rollback`, `api_undo`,
   `api_chat_apply`, `api_chat_rollback`, trash restore) and the matching
   `cmd_*` functions in `cli.py`, carrying paths, the one-line summary, and
   any trash/undo token.
3. Hub page: `activity.html` + `activity.js` served via the `_themed_page`
   pattern (like `settings_page`), one header link from `index.html`; the
   page also hosts the Trash panel, so the safety net has a single home.
   Relative-time and sentence formatting live in a pure JS helper module so
   `node --test` can cover them.
4. CLI: `mooring activity [--path <rel>]`.

## Testing

- **New `tests/test_trash.py`** — deposit/restore round-trip, slug collision
  (the `a/b.py` vs `a_b.py` case `test_notebook_undo.py` pins for undo),
  retention/prune (N-per-file, age, total cap), per-file size cap,
  byte-identical skip, supersession detection. Pure-local, no mocks.
- **Extend `tests/test_sync.py`** (GitHub mocked with `responses`, offline as
  today): resolve-THEIRS and pull-THEIRS deposit before overwrite;
  `SyncResult.trashed` populated; **invariant pin:** a workspace with trash
  entries and an `activity.jsonl` yields nothing new from `scan_local` —
  the safety net can never leak into a push.
- **Extend `tests/test_deletion.py` / `tests/test_cli_delete.py`**: delete
  deposits every removed file, including all members of a `.pbip` artifact.
- **Extend `tests/test_hub.py`**: `/api/trash` listing, restore happy path,
  409 on superseded restore, tokens present in `/api/resolve` and
  `/api/delete` responses, `/api/activity` filtering.
- **New `tests/test_activity.py`**: append/read/rotate; **invariant pin:**
  `activity.py` and `trash.py` import nothing above L0 (also enforced by
  adding both to the `foundation-is-pure` contract in `.importlinter`).
- **JS (`node --test tests/js/`)**: a new test file for the toast state and
  the relative-time/sentence formatter, following `files_tree.test.js`'s
  pure-helper pattern.

## Risks and mitigations

- **Disk growth.** A churned 40 MB data file could bank gigabytes in two
  weeks. Mitigations: per-file cap, total-size cap with oldest-first
  eviction, byte-identical skip, prune on every hub start. If field reports
  still show growth, flip the default to snapshotting `.py`/`.toml` only and
  make data files opt-in.
- **Snapshotting the wrong writes.** The trash must capture only
  mooring-initiated destruction. marimo's `--watch` autosaves and the user's
  own editor writes never route through `sync.py`/`deletion.py`, so hooking
  only those modules enforces this structurally — never hook a file watcher.
- **Two undo stores.** `notebook_undo` (the `.py` LIFO stack shared with AI
  Apply) and the trash (flat, token-addressed) could confuse each other.
  They stay separate: `.py` Revert keeps its existing snapshot+`/api/undo`
  path unchanged; trash restore is token-exact and refuses on supersession,
  so neither can restore the other's layer.
- **Privacy perception.** A local journal of filenames plus cached data-file
  bytes looks like surveillance if undocumented. The ledger and trash are
  strictly local, live in `.mooring/`, never sync (structural dot-dir
  exclusion), and are distinct from the opt-in central telemetry — stated
  plainly in [configuration](../../admins/configuration.md) and the user
  docs.
- **Cloud-sync workspaces.** Like `.mooring/undo/`, the trash inherits the
  workspace's fate under OneDrive/Dropbox (`paths.synced_folder_hint`
  already warns): it is a convenience, not durable history. The version
  history page covers the durable case.
- **UI creep.** The hub stays simple: one toast, one header link, no
  front-page timeline.

## Dependencies and sequencing

Standalone — no other roadmap page is a prerequisite, which is why it sits in
the "quick trust wins" tier with the [staleness guard](staleness-guard.md) and
[duplicate as draft](duplicate-as-draft.md). Relations to keep in mind:

- ["Recall last push"](push-guard.md) is deliberately **not** here — undoing a
  push is a remote operation and belongs to the push guard.
- [Version history](version-history.md) is the remote/durable complement:
  trash restores your last local pre-image; history restores anything ever
  pushed. Trash restore works offline; `sync.revert` needs `get_blob` —
  which also makes this a natural companion to [offline mode](offline-mode.md).
- The activity ledger's sentences get materially better once
  [review my changes](review-my-changes.md) ships push notes (today's commit
  messages are machine-generated), and a [pull digest](pull-digest.md) entry
  can land in the same ledger.
- [mooring doctor](mooring-doctor.md) should report trash size and prune
  failures once both exist.
