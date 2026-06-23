---
icon: lucide/refresh-cw
---

# Daily workflow

Everything below happens in the **hub** (the browser page that opens when you
run the app). The same actions are available from the
[command line](cli.md) if you prefer a terminal.

## The seven actions

| Action | What it does |
|--------|--------------|
| **Pull** | Download the team's latest notebooks and data. Never overwrites your local edits — changes that would collide are flagged as conflicts. |
| **Open** | Open a notebook in the bundled marimo editor (a new browser tab), or a Power BI project in **Power BI Desktop** — see [Power BI reports](power-bi.md). |
| **New notebook** | Create a fresh marimo notebook from a template and open it. |
| **Push** | Upload your changed files to the team repo — **one commit per file**. Blocked for any file that's in conflict. |
| **Propose** | Like Push, but uploads to a **review branch** instead of the shared branch, so a teammate can review the changes as a pull request before they land. See [Proposing changes](#proposing-changes-for-review). |
| **Revert** | Appears on a *modified* or locally-deleted file. Discards your local changes and restores the last version you pulled or pushed. Your current version is snapshotted first, so a Revert can itself be undone. See [Reverting a file](#reverting-a-file). |
| **Resolve** | Appears on conflicted files. See [Resolving conflicts](conflicts.md). |

Power BI projects appear as a **single grouped row** (expand with the ▸ caret
to see individual files); everything else is one row per file.

## A typical session

1. **Pull** first, so you start from the team's latest.
2. **Open** a notebook (or **New notebook** to start one), edit it in marimo,
   and save.
3. Back in the hub, your edited file shows as *modified*.
4. **Push** to share it. If someone changed the same file upstream since your
   last pull, the push is blocked and the file is marked *conflicted* —
   resolve it, then push again.

!!! tip "Pull before you push"

    Pulling regularly keeps conflicts small and rare. Mooring will never let a
    push silently overwrite a teammate's work — GitHub itself rejects a write
    whose base SHA is stale — so the worst case is a conflict you resolve, not
    lost work.

!!! tip "Ask the copilot (optional)"

    If your team has enabled it, open a notebook and click **AI** to chat with an
    assistant that proposes marimo cells. It's sent your column names and notebook
    source — never your data values — and you review every change before it lands.
    See [AI copilot](ai-copilot.md).

## Proposing changes for review

If your team prefers changes to be reviewed before they land, use **Propose**
instead of Push:

1. **Propose** uploads your changed files to a personal review branch
   (named like `mooring/your-username/20260612-0900`) — the shared branch is
   untouched.
2. The hub shows a link: **create / view the pull request**. Click it and
   press *Create pull request* on GitHub. That's the only step that happens
   on GitHub itself — mooring never opens the PR for you.
3. Proposed files show an *in review* badge. They are left out of **Push all**
   so you can't accidentally bypass the review.
4. Need to update the proposal after feedback? Edit the file and **Propose**
   again — it goes to the same branch and the open pull request updates
   itself.
5. When the pull request is **merged**, the badge clears on its own and a
   normal **Pull** brings your workspace in line. If the pull request is
   closed and its branch deleted instead, the files simply go back to
   *modified* — nothing is lost, and your next Propose starts a fresh branch.

!!! note "If a reviewer edits the pull request"

    The *in review* badge clears when your exact change lands on the shared
    branch. If a reviewer amends the PR before merging, the merged version
    differs from yours — the badge clears once the review branch is deleted
    (GitHub offers this right after merging), and the reviewer's version
    arrives with your next pull.

## Reverting a file

Changed a notebook and want to throw those edits away? **Revert** restores it to
the last version you pulled or pushed — your personal "go back to the last
checkpoint" button.

- Revert appears on a file that is **modified** (you edited it) or **deleted
  locally** (you removed it but it still exists in the team repo). It does *not*
  appear on a brand-new file that was never synced — there's no earlier version
  to go back to, so use **Delete** for that.
- The last-synced bytes are fetched from the team repo, so Revert needs you to be
  **logged in** (unlike Delete, which is purely local).
- Before overwriting, mooring snapshots your current version, so an **Undo**
  button appears on the row right after — click it to bring your edits back.
- Revert only touches the file you pick and only *your* local changes. It never
  changes the team repo and never undoes a teammate's pull. For a file in
  conflict, Pull first (or, on the command line, `rollback --conflicts` to drop
  your side and turn it into a clean pull, then Pull to take the team's version).

On the command line this is [`rollback`](cli.md#rollback).

## Switching repos

A team can share more than one repo (say, `notebooks` for the team and a
personal sandbox). Register each one once, then switch with the **dropdown in
the hub header** — the file list, workspace, and pull/push all follow the
selected repo. Choose **+ Add repo…** in the same dropdown to register another.

Each repo keeps its **own workspace folder** on disk and its own sync state,
so switching never mixes files. Your GitHub login covers all of them. The same
controls exist on the command line as [`repo` commands](cli.md#repo).

## Where your files live

Notebooks, data, and reports sync into your home folder:

=== "Windows"

    ```
    PythonProjects\mooring\<owner>\<repo>\notebooks\   ← .py notebooks
    PythonProjects\mooring\<owner>\<repo>\data\        ← data files your notebooks read
    PythonProjects\mooring\<owner>\<repo>\reports\     ← Power BI projects (.pbip)
    ```

=== "macOS / Linux"

    ```
    ~/PythonProjects/mooring/<owner>/<repo>/notebooks/
    ~/PythonProjects/mooring/<owner>/<repo>/data/
    ~/PythonProjects/mooring/<owner>/<repo>/reports/
    ```

`<owner>/<repo>` mirrors your team repository's GitHub address. All synced
folders work the same way, so a CSV your notebook reads from `data/` travels
with the notebook.

!!! warning "Keep big datasets out of the repo"

    Pushes **warn at 10 MB** and **refuse at 45 MB** per file (a GitHub
    Contents API limit). Store large or sensitive datasets elsewhere and have
    notebooks load them at runtime.

## What you can import in a notebook

The repo's notebook packages are declared in a `pyproject.toml` + `uv.lock` at the
workspace root, shared with the team through GitHub. Add to them with
`mooring deps add <pkg>` (then `mooring push`), and see the whole set with
`mooring deps list` — see the [CLI reference](cli.md#init-deps-notebook-dependencies).

With [uv](https://docs.astral.sh/uv/) on your machine, notebooks open in that
locked environment automatically. On a frozen `.pyz` with no uv, you can import
whatever your admin built in; opening a notebook that needs something the build
lacks shows a warning (ask your admin to add it — see
[Build & distribute](../admins/build-and-distribute.md)).
