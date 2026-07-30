---
icon: lucide/git-merge
---

# Resolving conflicts

Mooring never overwrites work silently. When the same file has been changed in
two places, it's flagged as a **conflict** and you decide how to resolve it.

## Why conflicts happen

Mooring keeps a manifest of what you last synced and computes a git blob SHA for
each local file, so it always knows whether a file is **modified locally**,
**changed remotely**, or **both** (a conflict). Concretely:

- **Pull** brings down remote changes but **never overwrites a local edit**. If
  a file changed both locally and remotely, pull leaves your copy alone and
  marks it conflicted.
- **Push** sends each file with its last-known SHA. If the remote moved on since
  your last pull, **GitHub rejects the write** and mooring marks the file
  conflicted instead of clobbering the remote.

So a conflict simply means: *both sides changed; pick what should win.*

## Merging a notebook cell by cell

Most conflicts aren't really a disagreement: two people edited **different
cells** of the same notebook. For those, **Merge cell by cell…** — the first
action on a conflicted notebook row — does the whole job.

It compares three versions of the notebook: the one you both last synced, your
copy, and the team's. Then:

- a cell **only one of you changed** is merged for you, whichever side changed
  it — that's usually every cell, and the panel tells you how many;
- a cell **you both changed** is the only thing you're asked about. You see the
  two versions side by side and pick one per cell;
- a cell **only one of you added** is kept — *both* cells survive when you each
  added one, the same as git would do; a cell **only one of you deleted** is
  dropped.

The notebook's header — its `# /// script` dependency block and its
`marimo.App(...)` settings — comes along too, so a teammate's dependency pin
isn't quietly reverted, and each merged cell keeps its own `@app.cell` settings
(a cell the team deliberately disabled stays disabled).

Nothing is preselected, and the **Write the merged notebook** button stays
disabled until every contested cell has an answer.

!!! note "Merging doesn't publish anything"

    The merge writes **your local file only**. Afterwards the notebook is a
    normal *modified* file — review it, run it, then **Push** it like any other
    change. It **does replace your working copy**, so your previous version is
    saved to the local trash first: the **Undo** toast — and the hub's Activity
    page, once the toast is gone — puts it straight back. If that copy can't be
    saved for any reason, the merge is refused rather than written.

### When mooring refuses to merge

A wrong merge is much worse than no merge, so mooring only offers this when it
can be honest about it. It steps aside — and leaves you the three resolutions
below — when the file isn't a marimo notebook, when either version can't be
read as cells, when you and the team created the file separately (there's no
shared version to merge against), when the notebook was restructured so heavily
that cells can't be lined up confidently, when it can't tell a *rewritten* cell
from a deleted one plus a new one, when you both changed the notebook's header,
or when the merged result would define the same name in two cells (which marimo
refuses to run).

## The three resolutions

On any conflicted file the hub offers:

| Choice | Result |
|--------|--------|
| **Use remote** | Discard your local edit and take the team's version. |
| **Keep both** | Keep your local edit **and** save the remote version alongside it as a copy, so nothing is lost and you can merge by hand. |
| **Push as copy** | Publish *your* version under a new name, `name-<your-github-login>.py`, leaving the original untouched. Good when both versions should survive as separate notebooks. |

!!! tip

    **Keep both** and **Push as copy** leave every file where it is, so reach
    for those when unsure. **Merge cell by cell** and **Use remote** do rewrite
    your working copy — but both save it to the trash first, so **Undo** brings
    it back.

## From the command line

The same strategies are available on `pull`:

```
mooring pull              # skip conflicts, leave them for you to resolve
mooring pull --theirs     # overwrite local edits with remote versions
mooring pull --keep-both  # keep local edits, save remote versions as copies
```

!!! note

    Running a frozen `.pyz`/`.exe` build? Use `python mooring.pyz pull` (or
    `mooring.exe pull`) instead.

A plain `pull` (no flag) downloads everything that's safe and **skips**
conflicted files so you can resolve them deliberately. See the
[CLI reference](cli.md).
