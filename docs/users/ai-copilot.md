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
2. Optionally pick a **dataset** (so the assistant knows your columns and types),
   a **model**, and a reasoning **effort**.
3. Ask for what you want — e.g. *"filter to 2024 and total `amount` by `region`"*.
   While it works you'll see a thinking indicator and a status line
   (*"Looking up the schema…"*); the reply then streams in with formatted code.
4. Click **Apply ▸** on a proposed cell: it's written into your notebook and runs
   there. Review it like any other cell.

Keep both tabs side by side: chat on one, the marimo notebook on the other.

!!! warning "Never paste real values"
    Anything you type into a cell or the chat is visible to the assistant. Refer
    to columns by name — don't paste actual data values.

## Tips

- Keep the notebook tab open beside the chat so applied cells appear live; if
  it's closed, the cell is still saved and shows next time you open the notebook.
- **Model & effort:** pick a model from the dropdown; for models that support it,
  a higher reasoning **effort** trades speed for more thorough answers. Your
  choice is remembered. (Some models — and "Auto" — have no effort setting.)
- The assistant can read your notebook's current code and ask for a dataset's
  schema on its own; you don't need to paste either in.
- It writes Polars (`pl`) by default, matching mooring's bundled notebook stack.
