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
- a cell **only one of you added** is kept; a cell **only one of you deleted**
  is dropped.

Nothing is preselected, and the **Write the merged notebook** button stays
disabled until every contested cell has an answer.

!!! note "Merging doesn't publish anything"

    The merge writes **your local file only**. Afterwards the notebook is a
    normal *modified* file — review it, run it, then **Push** it like any other
    change. Your previous copy goes to the local trash first, so the **Undo**
    toast — and the hub's Activity page, once the toast is gone — puts it
    straight back.

Mooring only offers this when it can be honest about it. If the file isn't a
marimo notebook, if either version can't be read as cells, if you and the team
created the file separately (so there's no shared version to merge against), or
if the notebook has been restructured so heavily that mooring can't line the
cells up confidently, it says so and leaves you the three resolutions below.

## The three resolutions

On any conflicted file the hub offers:

| Choice | Result |
|--------|--------|
| **Use remote** | Discard your local edit and take the team's version. |
| **Keep both** | Keep your local edit **and** save the remote version alongside it as a copy, so nothing is lost and you can merge by hand. |
| **Push as copy** | Publish *your* version under a new name, `name-<your-github-login>.py`, leaving the original untouched. Good when both versions should survive as separate notebooks. |

!!! tip

    There's no wrong choice you can't recover from — **Merge cell by cell**,
    **Keep both** and **Push as copy** are all non-destructive, so reach for
    those when unsure.

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
