---
icon: lucide/refresh-cw
---

# Daily workflow

**Pull** a teammate's notebook, edit it, and **share** your changes back —
all over GitHub with no git to learn and no personal access token to juggle,
just Python 3.12 or newer. Everything below happens in the **hub** (the browser
page that opens when you run the app). The same actions are available from the
[command line](cli.md) if you prefer a terminal.

!!! tip "Analyse your data with a copilot that never sees it"

    Open any notebook and click **AI** to chat with a schema-only assistant — it
    sees your column names and types and your notebook's code, but **never the data
    itself**: only column names and notebook source leave your machine, never the
    data values, cell outputs, or data-file contents. See
    [What the copilot can do](ai-copilot.md) and
    [why it cannot see your data](../admins/ai-privacy.md).

!!! note "Running a frozen build?"

    The CLI examples below assume the `uvx mooring` / PyPI install. Running a
    frozen `.pyz`/`.exe` build instead? Use `python mooring.pyz <cmd>` (or
    `mooring.exe <cmd>`) in place of `mooring <cmd>`.

## The main actions

| Action | What it does |
|--------|--------------|
| **Pull** | Download the team's latest notebooks and data. Never overwrites your local edits — changes that would collide are flagged as conflicts. |
| **Open** | Open a notebook in the bundled marimo editor (a new browser tab), or a Power BI project in **Power BI Desktop** — see [Power BI reports](power-bi.md). |
| **New notebook** | Create a fresh marimo notebook from a template and open it. A bare name lands in `notebooks/`; type a path (e.g. `packages/finance/notebooks/sales`) to place it in a sub-folder — mooring registers that folder so it syncs for the team. |
| **Deliver** | Render a notebook to a **self-contained HTML snapshot** (code hidden) you can email a stakeholder who won't open marimo. See [Delivering a result](#delivering-a-result-for-a-stakeholder). |
| **Verify runs** | Smoke-run the notebook once on your machine and badge the row with whether it **ran clean** — the "does this still run before I share it?" check. See [Verifying a notebook runs](#verifying-a-notebook-runs). |
| **Push** | Upload your changed files to the team repo — **one commit per file**. Blocked for any file that's in conflict. |
| **Propose** | Like Push, but uploads to a **review branch** instead of the shared branch, so a teammate can review the changes as a pull request before they land. See [Proposing changes](#proposing-changes-for-review). |
| **Revert** | Appears on a *modified* or locally-deleted file. Discards your local changes and restores the last version you pulled or pushed. Your current version is snapshotted first, so a Revert can itself be undone. See [Reverting a file](#reverting-a-file). |
| **Resolve** | Appears on conflicted files. See [Resolving conflicts](conflicts.md). |

Power BI projects appear as a **single grouped row** (expand with the ▸ caret
to see individual files); everything else is one row per file.

!!! tip "Finding a notebook in a growing repo"

    Each notebook shows its **title** — the first heading in its own first markdown
    cell — beneath its filename, so a file like `q3_recon_v2.py` is legible at a
    glance. Use the **filter box** above the list to find one by filename or title.
    Titles are read from the notebook's own text on your machine; nothing leaves it.

## A typical session

1. **Pull** first to grab a teammate's notebook and start from the team's latest.
2. **Open** it — it runs in the same locked environment they used — (or **New
   notebook** to start one), edit it in marimo, and save.
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

## Delivering a result for a stakeholder

Your manager wants the number and the chart — not a `.py`. **Deliver** renders a
notebook to a **self-contained HTML file** (the outputs, with the code hidden)
that you can double-click or attach to an email or Teams message.

1. On a notebook's **Actions ▾** menu, choose **Deliver**. Mooring runs the
   notebook once **on your machine** and saves the result to a local outbox
   (`.mooring/outbox/<notebook>/<name>-<date>.html`), then reveals it in your file
   manager and opens it for preview.
2. The file carries a small **provenance footer** — which repo and commit it came
   from, the notebook, the date, and a *View on GitHub* link — so a reader can
   trace it back.
3. Attach it wherever you like. On the command line this is
   [`mooring deliver <path>`](cli.md).

!!! warning "The HTML contains your data — it is never pushed"

    A rendered snapshot embeds real values, so mooring keeps it in the
    `.mooring` folder, which **never syncs** — it can't ride a push or be shared
    by accident. Sending it to a stakeholder is a deliberate step you take
    yourself.

## Checking your numbers tie out

A number is only trustworthy once it *ties out* — segment totals reconcile to a
control, a key is unique, a join didn't double your rows. In any notebook cell you
can assert these with the built-in **`mooring_checks`** helper:

```python
import mooring_checks as mc
mc.reset()                                  # start fresh each run
mc.reconciles(segment_total, control_total, tol=0.01)
mc.unique_key(loans, "loan_id")             # no duplicate keys
mc.no_fanout(loans, rates, on="rate_id")    # this join won't multiply rows
mc.not_null(loans, "balance")               # no missing balances
```

Each check prints a pass/fail line in your notebook and records a **value-free**
receipt (the check name and whether it passed — **never a data value**). The hub
shows a green **✓ N checks** badge on the notebook's row, or a red **✗ M failing**
badge if something doesn't tie out; `mooring checks` lists them from the terminal.

The badge reflects your **last run**. Starting the cell with `mc.reset()` keeps it
current; if you remove the checks cell entirely, clear the leftover badge with
`mooring checks --clear` (or `--clear <path>` for one notebook).

!!! tip "Let the copilot write them"

    Open the copilot and type **`/checks`** (or just ask). It reads your schema and
    source — never your data — and proposes a `mooring_checks` cell tailored to the
    notebook for you to review and apply. See [AI copilot](ai-copilot.md).

## Verifying a notebook runs

A notebook you inherited — or one you haven't opened in weeks — might not run any more:
a dependency moved, an input path changed, a cell was left half-edited. Before you share
its number, **Verify** it.

1. On a notebook's **Actions ▾** menu, choose **Verify runs**. Mooring runs the whole
   notebook once **on your machine**, top to bottom, in the same locked environment the
   editor uses, and records the outcome.
2. The row then shows a green **✓ ran clean** badge, or an amber **⚠ … failed to run**
   badge if a cell errored — open the notebook to see which one. On the command line
   this is [`mooring verify <path>`](cli.md).
3. The badge is tied to the notebook's **current contents**: the moment you edit the
   file, the badge **clears itself**, because "it ran clean" is no longer a claim about
   the code that's now there. Re-verify after your edits.

!!! info "Value-free, local, and never committed"

    Verify only records **whether** the notebook ran — a green/red boolean and a
    date, **never a value or an error message**. The run's rendered output (which *does*
    contain values) is written to the `.mooring` folder and deleted straight away, and
    the receipt stays on your machine — it never syncs and the AI never sees it.

!!! warning "A green badge means it *ran*, not that the number is *right*"

    Verify proves the notebook executes without error. It can't tell you the answer is
    correct — for that, tie your numbers out with
    [`mooring_checks`](#checking-your-numbers-tie-out) and review the logic with the
    copilot's [Review logic](ai-copilot.md#review-my-logic).

## Fingerprinting your inputs

*"Same inputs, same numbers?"* — the question an auditor (or you, three months later)
asks about a report. Pin the exact data a run read with the built-in **`mooring_inputs`**
helper, right after you load each input:

```python
import mooring_inputs as mi
mi.reset()                                               # start fresh each run

sales = pl.read_csv("data/sales.csv")
mi.fingerprint(sales, "sales", path="data/sales.csv")    # hash + shape + schema
```

Each call records a **value-free** fingerprint — the file's **content hash**, its
**shape** (row/column counts), and its **schema** (column names + types), **never a data
value** — and compares it to the previous run. If an input changed under you (different
content, more rows, a new column), the cell prints `[CHANGED] …` and the hub shows an
amber **⚠ input changed** badge on the notebook's row; otherwise a green **⛓ N inputs
pinned** badge. `mooring inputs` lists them from the terminal, and `mooring inputs --clear`
resets them.

Always pass **`path=`** to the source file — that's what gives the *content* guarantee
(the file hash catches a same-shape value change). Without a `path`, only the shape and
schema are compared. Starting the cell with `mi.reset()` keeps the badge honest if you
later rename or drop an input.

Because `mi.fingerprint(...)` returns falsy when the input changed, you can even make it a
guard:

```python
assert mi.fingerprint(sales, "sales", path="data/sales.csv"), "sales.csv moved — re-check the totals"
```

!!! info "Value-free, local, and never pushed"

    The fingerprint is a hash, two counts, and column names/types — never a value. The
    receipt lives in the `.mooring` folder, which **never syncs**, and the AI never sees
    it. (A container format like `.xlsx`/`.parquet` can re-compress to different bytes for
    the same data, so treat the hash as a *file* fingerprint, backed up by the shape and
    schema.)

!!! tip "Let the copilot add them"

    Ask the copilot to *"fingerprint the inputs"* — it reads your schema and source (never
    your data) and proposes the `mooring_inputs` cell for you to review and apply.

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

On the simple `uvx mooring` path, [uv](https://docs.astral.sh/uv/) is already
on your machine, so notebooks open in the team's locked environment automatically
— `mooring deps add <pkg>` and a push are all it takes to add a package for
everyone.

!!! note "Advanced: on a frozen `.pyz`/`.exe` with no uv"

    With a frozen build and no uv, you can import whatever your admin built into
    the bundle; opening a notebook that needs something the build lacks shows a
    warning (ask your admin to add it — see
    [Advanced: offline / frozen builds](../admins/build-and-distribute.md)).
