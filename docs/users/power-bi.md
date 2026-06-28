---
icon: lucide/chart-column
---

# Power BI reports

Mooring syncs **Power BI report projects** (`.pbip`) alongside your notebooks,
so report definitions live in the same GitHub repo and are shared with your
team — no git, no tokens. You keep authoring in Power BI Desktop; mooring
handles the sync.

!!! note "The schema-only AI guarantee is unaffected"

    Syncing PBIP projects changes nothing about the copilot's privacy: it
    still sees only your column names and types and your notebook's code, never
    the data itself. See [Why it cannot see your data](../admins/ai-privacy.md).

## Save your report as a project (.pbip)

Mooring syncs the **PBIP project format** — the text-based, version-control
friendly way Power BI Desktop saves reports (not the binary `.pbix`).

1. In Power BI Desktop, open your report.
2. **File → Save as**, choose the **`.pbip`** file type.
3. Save it inside your mooring workspace, in the `reports` folder:

    ```
    PythonProjects\mooring\<owner>\<repo>\reports\Sales.pbip
    ```

Desktop creates three things next to each other:

```
reports\
  Sales.pbip               ← small pointer file (this is what you open)
  Sales.SemanticModel\     ← your data model, as text (TMDL)
  Sales.Report\            ← the report layout, as text
```

!!! tip "Saving .pbip needs the preview switch (older Desktop versions)"

    If `.pbip` doesn't appear as a save option, enable
    **File → Options → Preview features → Power BI Project (.pbip) save option**
    and restart Desktop. Recent versions have it on by default.

## How it shows up in the hub

The hub groups the whole project into **one row** — named after the report,
with a file count and a single status badge. Click the **▸** caret to expand
the individual files (useful when resolving conflicts).

| Badge | Meaning |
|-------|---------|
| *synced* | Everything matches the team repo. |
| *modified* | You have local changes — **Push** on the row uploads just this project's changed files. |
| *remote changed* | A teammate pushed changes — **Pull** (toolbar) fetches them. |
| *mixed* | Changes in **both** directions (e.g. you edited the model, a teammate edited the layout). Pull first, then push. |
| *conflict* | The same file changed on both sides. Expand the row and resolve per file — see [Resolving conflicts](conflicts.md). |

**Open** on the row launches the project in **Power BI Desktop** (Windows
file association — Desktop must be installed on the machine).

!!! note "Pushes take about a second per file"

    A PBIP project is many small files, and mooring pushes one commit per file
    with a short pause between writes (GitHub rate limits). A 30-file project
    takes ~25 seconds — the hub shows an estimate and the buttons stay
    disabled until it finishes.

## What syncs (and what doesn't)

Mooring syncs all the project's text files, **including** each folder's
`.platform` metadata file (required by the format despite the leading dot).

It deliberately **never** syncs the `.pbi\` folders inside the project —
they hold machine-local state (`localSettings.json`, `unappliedChanges.json`,
and the `cache.abf` data cache, which can be tens of MB). Power BI Desktop
recreates them on open, and teammates pulling your report won't receive your
local cache.

## Conflicts in Power BI files

The expanded row gives you the same per-file conflict tools as notebooks
(*Use remote*, *Keep both*, *Push as copy*). TMDL and report JSON are text,
but they're machine-written — after taking a remote version, open the project
in Desktop and re-save to make sure everything is consistent before pushing
again.
