---
icon: lucide/sparkles
---

# AI copilot

The copilot is an interactive assistant that helps you write notebook code. It
chats with you in a second browser tab beside your notebook and can **propose
cells** that you apply with one click. It is **schema-only**: it sees your
columns' names and types and your notebook's code, but never the data itself —
see [why the copilot can't see your data](../admins/ai-privacy.md).

## One-time setup

The copilot ships as an optional extra (it bundles GitHub's Copilot CLI):

```
pip install "mooring[copilot]"      # or: uvx "mooring[copilot]"
mooring ai login                    # sign in to GitHub Copilot (opens a browser)
mooring ai status                   # check you're connected
```

You need a GitHub Copilot licence, and your organisation must have the Copilot
CLI/agent policy enabled. If the extra isn't installed, the chat will tell you.

## Using it

1. In the hub, open a notebook (**Open**) and, on the same row, click **AI** —
   the chat opens in a new tab.
2. Optionally pick a **dataset** so the assistant knows your columns and types.
3. Ask for what you want — e.g. *"filter to 2024 and total `amount` by `region`"*.
   The reply streams in, and code suggestions appear as **Proposed cell** blocks.
4. Click **Apply ▸** on a proposal: the cell is added to your open notebook **and
   run**. Review it there like any other cell.

Keep both tabs side by side: chat on one, the marimo notebook on the other.

!!! warning "Never paste real values"
    Anything you type into a cell or the chat is visible to the assistant. Refer
    to columns by name — don't paste actual data values.

## Tips

- If **Apply** says *"open the notebook tab first"*, the copilot couldn't find
  your notebook tab — make sure it's open, then apply again.
- The assistant can read your notebook's current code and ask for a dataset's
  schema on its own; you don't need to paste either in.
- It writes Polars (`pl`) by default, matching mooring's bundled notebook stack.
