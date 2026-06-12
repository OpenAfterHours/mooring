---
icon: lucide/refresh-cw
---

# Daily workflow

Everything below happens in the **hub** (the browser page that opens when you
run the app). The same actions are available from the
[command line](cli.md) if you prefer a terminal.

## The six actions

| Action | What it does |
|--------|--------------|
| **Pull** | Download the team's latest notebooks and data. Never overwrites your local edits — changes that would collide are flagged as conflicts. |
| **Open** | Open a notebook in the bundled marimo editor (a new browser tab), or a Power BI project in **Power BI Desktop** — see [Power BI reports](power-bi.md). |
| **New notebook** | Create a fresh marimo notebook from a template and open it. |
| **Push** | Upload your changed files to the team repo — **one commit per file**. Blocked for any file that's in conflict. |
| **Propose** | Like Push, but uploads to a **review branch** instead of the shared branch, so a teammate can review the changes as a pull request before they land. See [Proposing changes](#proposing-changes-for-review). |
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

## Proposing changes for review

If your team prefers changes to be reviewed before they land, use **Propose**
instead of Push:

1. **Propose** uploads your changed files to a personal review branch
   (named like `mooring/your-username/20260612-0900`) — the shared branch is
   untouched.
2. The hub shows a link: **create / view the pull request**. Click it and
   press *Create pull request* on GitHub. That's the only step that happens
   on github.com — mooring never opens the PR for you.
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
    Documents\mooring\<owner>\<repo>\notebooks\   ← .py notebooks
    Documents\mooring\<owner>\<repo>\data\        ← data files your notebooks read
    Documents\mooring\<owner>\<repo>\reports\     ← Power BI projects (.pbip)
    ```

=== "macOS / Linux"

    ```
    ~/Documents/mooring/<owner>/<repo>/notebooks/
    ~/Documents/mooring/<owner>/<repo>/data/
    ~/Documents/mooring/<owner>/<repo>/reports/
    ```

`<owner>/<repo>` mirrors your team repository's GitHub address. All synced
folders work the same way, so a CSV your notebook reads from `data/` travels
with the notebook.

!!! warning "Keep big datasets out of the repo"

    Pushes **warn at 10 MB** and **refuse at 45 MB** per file (a GitHub
    Contents API limit). Store large or sensitive datasets elsewhere and have
    notebooks load them at runtime.

## What you can import in a notebook

Notebooks can import anything frozen into the app, plus the standard library:

`polars`, `altair`, `plotly`, `openpyxl`, `fastexcel`, `requests`

There is **no pip at runtime** — if you need another package, ask your admin to
add it to the build (see [Build & distribute](../admins/build-and-distribute.md)).
