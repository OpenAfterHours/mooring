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

## The three resolutions

On a conflicted file the hub offers:

| Choice | Result |
|--------|--------|
| **Use remote** | Discard your local edit and take the team's version. |
| **Keep both** | Keep your local edit **and** save the remote version alongside it as a copy, so nothing is lost and you can merge by hand. |
| **Push as copy** | Publish *your* version under a new name, `name-<your-github-login>.py`, leaving the original untouched. Good when both versions should survive as separate notebooks. |

!!! tip

    There's no wrong choice you can't recover from — **Keep both** and
    **Push as copy** are non-destructive, so reach for those when unsure.

## From the command line

The same strategies are available on `pull`:

```
python mooring.pyz pull              # skip conflicts, leave them for you to resolve
python mooring.pyz pull --theirs     # overwrite local edits with remote versions
python mooring.pyz pull --keep-both  # keep local edits, save remote versions as copies
```

A plain `pull` (no flag) downloads everything that's safe and **skips**
conflicted files so you can resolve them deliberately. See the
[CLI reference](cli.md).
