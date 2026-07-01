---
icon: lucide/copy
---

# Duplicate as draft and a first-run checklist

!!! note "Status: proposed"
    Designed 2026-07 from a multi-agent ideation review. Not yet implemented;
    scope and naming may change.

## Problem

New and cautious analysts are afraid of wrecking the team's notebook. The shared-repo
model creates the fear: every file in the hub is *the* file, so the only way to
experiment is to edit the thing everyone else pulls. In practice people either don't
experiment at all, or they edit the shared notebook and agonise before pushing — or they
improvise a copy in Explorer, which lands as an oddly-named `new local` file they don't
understand and later push by accident.

Mooring already has everything needed to make a safe copy: a duplicate is just a new
local file to the three-way sync engine. `sync.classify` puts a file with no manifest
base and no remote SHA into `NEW_LOCAL` — it can never conflict with the team file, it
carries a `push` badge but reaches the repo only when the user chooses to share it, and `mooring delete`
(or the row's Delete action) discards it cleanly. What's missing is a one-click action
that makes the copy with a name that signals ownership and dodges the filename traps.

The second half of the same onboarding problem: a fresh, logged-in workspace gives no
hint of the working rhythm (pull, open, experiment on a draft, push or propose). The
docs explain it, but analysts are non-developers and mostly won't read them. A small
in-hub checklist that ticks itself off as the user does each step is the first-week ramp
the product itself can provide.

## Design

### Duplicate as draft

A new per-row action, **Duplicate as draft**, on every notebook row in the hub (and a
new `mooring duplicate <path>` CLI command). It copies the notebook byte-for-byte into
the *same folder* as:

```
notebooks/sales-report.py  →  notebooks/sales-report-phil-draft.py
```

where `phil` is the logged-in GitHub login (in the hub's local, no-repo mode the suffix
is just `-draft`). The copy opens immediately in the marimo editor, so the gesture is
"give me a safe playground for this notebook, now". On a name collision the suffix gains
a counter (`-draft-2`, `-draft-3`, …), the same de-duplication loop `create_unique`
already uses.

The naming follows a precedent that already exists in the code: `sync.resolve`'s
`PUSH_COPY` strategy publishes a conflict copy as `{stem}-{username}{suffix}`. The
ideation sketch suggested `sales_report__phil-draft.py`; the plan adapts to
`{stem}-{login}-draft.py` to match the `PUSH_COPY` idiom and because the always-present
hyphen buys a structural guarantee (below).

Why this exact shape:

- **In place, not a `drafts/` folder.** Every listed notebook lives under a synced
  folder (`sync.synced_paths` walks `cfg.folders`), so a sibling copy automatically
  participates in sync and appears in the hub with no second listing source. A dedicated
  `drafts/` folder would need registering via `workspace_config.add_extra_folder`, and
  the `_excluded_by_patterns` docstring in `src/mooring/sync.py` already warns that a
  bare `drafts` exclude pattern hides a top-level folder of that name — an avoidable
  trap.
- **The shadow guard cannot fire on a draft.** `shadow.scan` skips any stem that is not
  a Python identifier (`if not stem.isidentifier() ...`), and the `-draft` suffix
  guarantees a hyphen in the stem. A draft of `polars.py` (already badged) becomes
  `polars-phil-draft.py`, which is un-importable and therefore harmless.
- **No collision with mooring's own markers.** The name never contains
  `sync.REMOTE_COPY_MARKER` (`.remote-`), never starts with a dot (so
  `sync.is_synced_path` keeps it), and never matches the `__*__.py` dunder rule in
  `notebook_template.opens_as_notebook`.
- **Byte-for-byte copy.** No marimo parse, no codegen, no re-encoding (so no UTF-8 BOM
  risk). The markdown title inside still names the original; the filename carries the
  ownership signal.

Guards, mirroring `notebook_template.create_from_input` and `Hub._open`: the source must
resolve inside the workspace, must be a notebook by `opens_as_notebook` (a plain helper
module or `__init__.py` is refused — the hub only offers the action on `is_notebook`
rows anyway, but the server backstops), and the *target* name must pass
`sync.is_synced_path` against `cfg.exclude`, so a team exclude pattern like `*-draft.py`
produces a clear error instead of an invisible file.

The draft then behaves like any file: `new local` badge, per-row Push/Propose/Delete
through the existing actions menu, and `View on GitHub` once pushed. Pushing it is
*visible* sharing — the filename says whose it is. Deleting it needs no sync step at all
until it has been pushed. One nudge is added against accidental sharing: the toolbar
**Push all** / **Propose** handlers in `app.js` get a `confirm()` when the outgoing
set includes `-draft.py` files ("Include your N draft(s)?"), consistent with
`deleteAction`'s existing confirm idiom. Pushing a draft from its own row stays
unprompted — that click is already explicit.

Teams that never want drafts in the repo can set a `[sync] exclude` pattern
(`*-draft.py`), with the documented caveat that excluded files disappear from the hub
listing entirely (`is_synced_path` filters both `scan_local` and `local_report`).

### First-run checklist

A small dismissible card on the hub index, shown when `state.logged_in` and
`state.mode === "repo"`, with four self-checking items:

1. **Pull the team's notebooks** — checks itself when any file row carries a
   remote-tracked state (anything other than `local` / `new local`).
2. **Open a notebook** — checked when `openAction` in `app.js` succeeds.
3. **Duplicate a draft to experiment safely** — checked when the new Duplicate action
   succeeds (or a `-draft.py` row exists).
4. **Push or propose a change** — checked in `pushAction`/`proposeAction` on a
   successful response (the `/api/push` / `/api/propose` body carries only `lines` and
   a `summary` string, not per-file counts), or when the review banner (`state.review`)
   is present.

Progress is stored client-side in `localStorage` under a per-repo key
(`mooring.checklist.<slug>`), the same pattern the theme already uses (`LS_THEME`).
The card hides once all items are checked or the user dismisses it. No backend state,
no new endpoints: `/api/state` already carries everything derivable, and the rest is
"the user did it in this hub". Experienced users on a new machine see it once and
dismiss it.

## Architecture fit

Everything sits in existing layers; **no `.importlinter` change is needed**.

- **`src/mooring/notebook_template.py`** (imported today by both adapters and already
  lazily importing `sync` + `workspace_config`) gains a new function
  `duplicate_as_draft(workspace, rel_path, *, owner, exclude) -> str`. It reuses the
  module's exclusive-create (`open(target, "x")`) and path-escape idioms. No new
  imports, so no new edges.
- **`src/mooring/hub/server.py`** (L4) gains a new endpoint `POST /api/duplicate`
  (handler `api_duplicate`, registered in `create_app` beside `/api/new`), shaped like
  `api_new`: 400 on `ValueError`/`FileExistsError`, then `return self._open(rel_path)`.
  Owner comes from `Hub.username()`, falling back to `""` on `AuthFailed` (local mode).
- **`src/mooring/cli.py`** (L4) gains a `duplicate` subcommand and `cmd_duplicate`,
  mirroring `cmd_new` (create, print, `cmd_open`).
- **`src/mooring/hub/static/`**: a row action in `fileActions` in `app.js` (rendered
  through the existing `actionsMenu` dropdown), the push-all confirm, a checklist card
  in `index.html`, and a new pure helper `checklist.js` following the
  `files_tree.js` pattern (`window.X` + `module.exports` for `node --test`).
- **Untouched:** `sync.py`, `manifest.py`, `github.py`, `editor.py`, and all of `ai/` —
  the copy is created before sync ever sees it, and no AI channel is involved, so the
  value-blindness property is unaffected. No network calls are added; the frozen build
  needs nothing new.

## Implementation plan

### Phase 1 — duplicate core (S)

1. Add `duplicate_as_draft(workspace, rel_path, *, owner, exclude)` to
   `src/mooring/notebook_template.py`: normalize `rel_path`, resolve-and-contain (the
   `create_from_input` idiom), read bytes, refuse non-notebooks via
   `opens_as_notebook`, build `{stem}-{owner + '-' if owner else ''}draft.py`
   (collapsing an existing `-draft(-N)` suffix so a draft-of-a-draft becomes `-draft-2`,
   not `-draft-draft`), check `sync.is_synced_path` on the target, then the
   `create_unique`-style `"x"`-mode loop.
2. Add `api_duplicate` to `src/mooring/hub/server.py` and register
   `Route("/api/duplicate", hub.api_duplicate, methods=["POST"])` in `create_app`.
3. Wire the row action in `fileActions` in `src/mooring/hub/static/app.js`, gated on
   `isNotebook && file.has_local` (never on PBIP member rows).
4. Add the `duplicate` parser and `cmd_duplicate` in `src/mooring/cli.py`.

### Phase 2 — draft-aware bulk push (S)

1. In `app.js`, extend the `btn-push` / `btn-propose` handlers (which already compute
   the candidate count from `lastFiles`) to `confirm()` when candidates include
   `-draft.py` paths.
2. Document the `[sync] exclude` escape hatch and its listing caveat in
   [configuration](../../admins/configuration.md).

### Phase 3 — first-run checklist (M)

1. New `src/mooring/hub/static/checklist.js`: pure functions
   `derive(files, review, stored)` → item states, and storage-key helpers; exported the
   `files_tree.js` way.
2. Add the card markup to `src/mooring/hub/static/index.html` (beside `files-card`) and
   render/hide it from `refresh()` in `app.js`; mark items done in `openAction`, the new
   duplicate action, and `pushAction`/`proposeAction` on success.
3. Style in `style.css` using the existing card/badge tokens.

## Testing

Offline throughout; GitHub is mocked with the suite's `FakeClient` (`tests/conftest.py`)
and monkeypatching, as the existing hub and sync tests do (`responses` is only used at
the REST-client layer, in `tests/test_github.py` / `tests/test_auth.py`).

- **`tests/test_notebook_template.py`** — new cases: draft naming (with/without owner),
  collision counter, draft-of-a-draft collapse, refusal of modules / `__init__.py` /
  workspace-escaping paths / exclude-hidden targets, byte-identical content, and the
  pinning test that a generated draft stem is never `str.isidentifier()` (the shadow
  immunity).
- **`tests/test_shadow.py`** — pin that `polars-phil-draft.py` is never flagged by
  `shadow.scan`.
- **`tests/test_sync.py`** — pin the safety invariant: a duplicated file classifies
  `NEW_LOCAL` next to a `MODIFIED` original, and pushing only the draft path leaves the
  original's manifest entry and remote path untouched.
- **`tests/test_hub.py`** — endpoint tests in the style of
  `test_local_mode_new_lists_and_opens_without_login` and
  `test_new_rejects_a_path_outside_the_workspace`: duplicate in local mode (no login,
  `-draft` suffix), duplicate in configured mode (owner suffix, row shows `new local`),
  traversal and module-source rejections, 404 on a remote-only source (`has_local`
  false).
- **`tests/js/`** (`node --test tests/js/`) — new `checklist.test.js` for
  `checklist.js`'s pure derivation (item states from file rows + stored flags,
  dismissal, per-repo keying).

## Risks and mitigations

- **Draft clutter in the shared repo.** If everyone pushes drafts, the repo fills with
  `-draft.py` files. Mitigations: the ownership-signalling name (clutter is at least
  attributable), the bulk-push confirm, the `[sync] exclude` pattern for strict teams,
  and pointing draft-sharing at Propose (a review branch) rather than Push. Accepted
  residual: mooring does not police what teammates push.
- **No merge-back.** A draft never flows back into the original automatically; adopting
  draft work means copying it across (or pushing the draft and deleting the original).
  This is stated in the UI copy. [Review my changes](review-my-changes.md) and
  [version history](version-history.md) make the manual step legible; a cell-level
  merge is explicitly out of scope.
- **Excluded drafts vanish.** A team `*-draft.py` exclude makes drafts invisible to the
  hub (single listing source is `sync`-scoped by design). The server refuses to *create*
  an excluded draft with a clear error, so the file can't silently disappear at birth —
  but a pattern added later hides existing drafts. Documented, and
  [mooring doctor](mooring-doctor.md) is the natural place to surface orphaned local
  files.
- **Naming edge cases.** Long stems plus a long login can produce unwieldy names
  (cosmetic), and a login is unavailable in local mode (falls back to `-draft`). The
  dunder, dotfile, `.remote-` and shadow traps are dodged structurally, with pinned
  tests rather than convention.
- **Checklist state is per-browser.** `localStorage` clears with browser data and does
  not roam. Acceptable: the checklist is a ramp aid, not a record; derivable items
  (pull, draft-exists) re-check themselves from `/api/state`.

## Dependencies and sequencing

Standalone — no other roadmap page blocks it, and Phase 1 is shippable alone.
Relations:

- [Push guard](push-guard.md): the Phase 2 bulk-push confirm is a tiny, draft-specific
  cousin of the push guard's pre-push seam; if the push guard ships first, the draft
  question should move into its dialog instead of a bare `confirm()`.
- [Local safety net](local-safety-net.md) and [version history](version-history.md):
  drafts reduce the *need* for recovery by moving experiments off the shared file, but
  don't replace it — an analyst who edits the original anyway still wants those.
- [Review my changes](review-my-changes.md): the natural "promote my draft" flow —
  propose the draft, or review the diff before folding it back by hand.
- The checklist's fourth item teaches the same push/propose moment the
  [pull digest](pull-digest.md) and push note build on; no code dependency.

See also the [architecture overview](../index.md) and
[contributing](../contributing.md).
