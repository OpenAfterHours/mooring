---
icon: lucide/refresh-cw
---

# Daily workflow

Everything below happens in the **hub** (the browser page that opens when you
run the app). The same actions are available from the
[command line](cli.md) if you prefer a terminal.

## The five actions

| Action | What it does |
|--------|--------------|
| **Pull** | Download the team's latest notebooks and data. Never overwrites your local edits — changes that would collide are flagged as conflicts. |
| **Open** | Open a notebook in the bundled marimo editor (a new browser tab). Edit and save there; mooring tracks the change. |
| **New notebook** | Create a fresh marimo notebook from a template and open it. |
| **Push** | Upload your changed files to the team repo — **one commit per file**. Blocked for any file that's in conflict. |
| **Resolve** | Appears on conflicted files. See [Resolving conflicts](conflicts.md). |

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

## Where your files live

Notebooks and data sync into your home folder:

=== "Windows"

    ```
    Documents\mooring\<repo>\notebooks\   ← .py notebooks
    Documents\mooring\<repo>\data\        ← data files your notebooks read
    ```

=== "macOS / Linux"

    ```
    ~/Documents/mooring/<repo>/notebooks/
    ~/Documents/mooring/<repo>/data/
    ```

`<repo>` is the name of your team's shared repository. Both folders sync the
same way, so a CSV your notebook reads from `data/` travels with the notebook.

!!! warning "Keep big datasets out of the repo"

    Pushes **warn at 10 MB** and **refuse at 45 MB** per file (a GitHub
    Contents API limit). Store large or sensitive datasets elsewhere and have
    notebooks load them at runtime.

## What you can import in a notebook

Notebooks can import anything frozen into the app, plus the standard library:

`polars`, `altair`, `plotly`, `openpyxl`, `fastexcel`, `requests`

There is **no pip at runtime** — if you need another package, ask your admin to
add it to the build (see [Build & distribute](../admins/build-and-distribute.md)).
