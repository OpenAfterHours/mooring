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
| **Check all run** | The same check for **every** notebook in the workspace, one at a time, with a summary of what still runs. Slow, and cancellable. See [Checking that everything still runs](#checking-that-everything-still-runs). |
| **Schedule refresh…** | Re-run a notebook on a cadence (pull → run → report), so you stop having to remember. Appears once the notebook has verified clean. See [Refreshing a notebook on a schedule](#refreshing-a-notebook-on-a-schedule). |
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

## Checking that *everything* still runs

Verify asks about one notebook. **Check all run** asks about the whole repo — useful
after you change a shared package, before a month-end, or when you inherit somebody's
folder and want to know what you're walking into.

Click **✓ Check all run** in the toolbar (or run
[`mooring verify --all`](cli.md#deliver-verify-checks-inputs)). Mooring tells you how
many notebooks it is about to run, then runs them **one at a time** on your machine and
reports:

> `12 notebooks: 10 ran clean, 1 failed, 1 could not run.`

* **ran clean** — it executed top to bottom.
* **failed** — it ran, but a cell errored. Open it to see which.
* **could not run** — it never started, usually because the environment is broken (a
  package the lock file no longer provides). That's not the notebook's fault, so it
  doesn't get a red badge — but it does mean nobody can use it right now.

A broken notebook never stops the sweep, and each notebook records exactly the same
**✓ ran clean** badge a hand Verify does — so the rows badge as normal, and each badge
still clears itself the moment you edit that file. `--resume` skips notebooks that are
already badged, so a run you stopped halfway is cheap to finish.

!!! info "It's slow, and you can stop it"

    Every notebook is executed for real, in sequence — minutes, not seconds. Progress
    shows while it runs and **Cancel** stops it after the notebook that's currently
    running. Notebooks it never got to are reported as *skipped*, never quietly counted
    as fine.

!!! warning "The summary ages the same way a badge does"

    "10 ran clean" is a claim about the exact notebooks that ran. Edit one and it drops
    out of the count (`1 edited since (no longer covered)`) instead of sitting there
    vouching for code nobody ran. And as with a single Verify: it proves each notebook
    **ran**, not that its numbers are right.

## Changing the team's packages

`mooring deps add`/`remove`/`lock` rewrites `uv.lock`, which is the environment
**everybody's** notebooks run in. It's the easiest way for one person to break the whole
team, so mooring puts two things in the way:

1. **Straight after the change**, it offers to check: *"uv.lock changed — 12 notebooks
   run against it. Check they still run?"* That's the sweep above, and it's the cheapest
   moment to find out what moved.
2. **Before the change is pushed**, the result is shown: pushing an unchecked or
   known-broken `uv.lock` stops with *"this dependency change breaks 3 notebooks — push
   anyway?"*. The lock is held back; everything else in the push still goes.

It **warns, it doesn't block** — "Push anyway" (or `--acknowledge-findings`) always
gets you through, deliberately, so the decision is yours and it's on the record.

!!! info "Why a green Verify badge isn't enough here"

    A badge is tied to the *notebook's* contents, not to `uv.lock`. Change the packages
    and every badge stays green over an environment nothing has been run against. So the
    check is tied to the exact lock file it was run against — swap the lock and mooring
    asks again rather than trusting a check that never saw it.

## Refreshing a notebook on a schedule

A month-end board pack, a daily reconciliation, a weekly exception report — the same
notebook run against new data, on a cadence. **Schedule** it and stop having to remember.

On a notebook's **Actions ▾** menu, choose **Schedule refresh…**, pick how often
(daily / every weekday / weekly / hourly) and at what time, and save. A **Scheduled
refreshes** card then appears below your files.

Each refresh **pulls the team's latest**, runs the notebook on your machine, and reports
three things — none of which is a data value:

| It reports | From |
|------------|------|
| whether it **ran** | the run itself |
| whether your numbers **still tie out** | your [`mooring_checks`](#checking-your-numbers-tie-out) cell |
| whether an **input changed** | your [`mooring_inputs`](#fingerprinting-your-inputs) cell |

That middle row is the point: *"your daily reconciliation ran at 07:30, but segment totals
no longer reconcile"* is worth more than the file it produces.

!!! info "When a refresh actually runs"

    By default, refreshes run **while the mooring hub is open** — it catches up on anything
    due the moment you open it, and keeps to the cadence while it stays open. Nothing is
    installed and nothing needs admin rights.

    To have them run **with the hub closed**, click **Run in the background** on the
    schedules card (or `mooring schedule background enable`). mooring registers a Windows
    scheduled task if your machine allows it, and otherwise falls back to a sign-in agent
    started from your own Startup folder — **neither needs admin rights**, and it tells you
    which one you got. If your machine permits neither, refreshes keep working through the
    hub; the card always says which clock is running.

    Open mooring after a week away and a daily schedule runs **once**, not seven times.

!!! warning "A schedule can never go stale quietly"

    If a refresh doesn't happen when it was due, the card goes **amber** and says so. And
    every HTML snapshot a schedule produces is stamped with its cadence and its **next due
    date** in the footer — so someone you emailed it to three weeks ago can see it's out of
    date without asking you.

**Before you can schedule a notebook it has to have [verified](#verifying-a-notebook-runs)
clean** — mooring won't schedule something that has never been shown to work. Editing a
scheduled notebook clears that verification, so it gets one retry instead of three until it
runs clean again.

A refresh **never pushes**. It only pulls, runs, and writes to your own machine — so a
refresh that goes wrong can't touch the team repo. If it can't reach GitHub it runs against
your local copy and says so rather than failing. If the run fails, your last good snapshot
is left exactly as it was; after a few consecutive failures the schedule pauses itself and
tells you. Fix the notebook, hit **Run now**, and it re-arms itself.

On the command line this is [`mooring schedule`](cli.md#schedule) and
[`mooring refresh`](cli.md#refresh).

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
## Connecting to a database

Pulling from a warehouse (Snowflake, SQL Server, …)? mooring lets the team share the
**connection details** without anyone's **password** ever leaving their machine — the
two are kept structurally apart.

Define the connection's **shape** once; it travels with the repo like any synced setting:

```bash
mooring connections add warehouse kind=snowflake account=acme-eu \
        database=ANALYTICS warehouse=REPORTING_WH role=ANALYST
```

Then each teammate stores their **own** secret **locally** — it is never synced:

```bash
mooring connections set-secret warehouse   # prompts; saved under .mooring, never pushed
```

(or set a `MOORING_CONN_WAREHOUSE_SECRET` environment variable, or use integrated auth and
store no secret at all).

In a notebook, assemble the two at run time:

```python
import mooring_connections as mc
c = mc.get("warehouse")

import snowflake.connector
conn = snowflake.connector.connect(
    account=c.account, database=c.database, warehouse=c.warehouse, role=c.role,
    user="svc_analyst",
    password=c.secret,   # resolved locally — never in the repo, the source, or the AI
)
```

The copilot can see the connection **shape** (its name and fields) and will write this
wiring for you — but it never sees `c.secret`.

!!! warning "The secret is refused from the synced file — by construction"

    mooring **refuses** to write a `password`/`token`/`key`-shaped field into the synced
    `mooring.toml` (`mooring connections check` flags any that slipped in by hand), and the
    local secret lives under `.mooring`, which **never syncs**. mooring does **not** install
    database drivers — your own environment supplies `snowflake-connector` / `pyodbc` / etc.
    (add them with `mooring deps add`).

## Proposing changes for review

If your team prefers changes to be reviewed before they land, use **Propose**
instead of Push:

1. **Propose** uploads your changed files to a personal review branch
   (named like `mooring/your-username/20260612-0900`) — the shared branch is
   untouched.
2. Mooring **opens the pull request for you** and the hub links straight to it
   (**View pull request #N**). It lands in your teammates' [Reviews
   inbox](#reviewing-a-teammates-changes) automatically — nobody has to touch
   GitHub. (Prefer to open it yourself? Turn off **Open the pull request on
   Propose** in Settings, and you'll get the compare link instead.)
3. Proposed files show an *in review* badge. They are left out of **Push all**
   so you can't accidentally bypass the review.
4. Need to update the proposal after feedback? Edit the file and **Propose**
   again — it goes to the same branch and the open pull request updates
   itself.
5. When the pull request is **merged**, the badge clears on its own and a
   normal **Pull** brings your workspace in line. If the pull request is
   closed and its branch deleted instead, the files simply go back to
   *modified* — nothing is lost, and your next Propose starts a fresh branch.

!!! note "Propose from the machine you started on"

    The review branch is tracked locally (in `.mooring`), so proposing the *same* change
    from a **second machine** starts a fresh branch and opens a **second** pull request.
    Keep a given proposal on one machine, or close the extra PR on GitHub.

!!! note "If a reviewer edits the pull request"

    The *in review* badge clears when your exact change lands on the shared
    branch. If a reviewer amends the PR before merging, the merged version
    differs from yours — the badge clears once the review branch is deleted
    (GitHub offers this right after merging), and the reviewer's version
    arrives with your next pull.

## Reviewing a teammate's changes

When someone on your team **Proposes** a change, you can approve it **without leaving
mooring** — no GitHub UI, no git. Open **Reviews** in the header:

1. The inbox lists open proposals awaiting review — everyone's except your own (GitHub
   won't let you approve your own change, which is the point of four-eyes).
2. Click one to see a **cell-aware diff**: for a marimo notebook, exactly which *cells*
   changed, were added, or removed — the same value-free view as **Review changes**, so
   no data ever leaves the machine.
3. Leave one **note** for the whole change and click **Approve** or **Request changes**
   (a note is required to request changes). mooring posts it as a review on the pull
   request; the author sees it on GitHub and, once approved, can merge.

!!! info "You review the PR that exists"

    The inbox shows proposals that already have a pull request open (the author created it
    from their Propose link). It reviews only mooring's own review branches
    (`mooring/<name>/…`), never unrelated PRs. Approving or requesting changes uses the
    same GitHub sign-in you already have — no extra permission to grant.

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
