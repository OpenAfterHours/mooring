---
icon: lucide/rocket
title: Quickstart
---

# Quickstart: from zero to working in 5 minutes

> Analyse data with an AI copilot that never sees your data — only column names,
> types, and your notebook's code ever go to the model, never the values
> themselves.

This is the whole journey end to end: install, connect to your team, pick up a
teammate's notebook, share your own work back, and ask the copilot — on nothing
but **Python 3.12+**. No git, no tokens to juggle.

## 1. Install — `uvx mooring`

With [uv](https://docs.astral.sh/uv/) and **Python 3.12 or newer**:

```bash
uvx mooring                  # the base app, or:
uv tool install mooring      # …install it as a persistent CLI
```

Want the AI copilot (and optional offline PII / name scanning)? Add an extra —
**quote the brackets** so your shell doesn't glob them:

```bash
uv tool install "mooring[copilot]"            # + the schema-only AI copilot
uv tool install "mooring[copilot,pii-spacy]"  # + offline PII & name detection
```

!!! note "Installing an extra doesn't switch the feature on"

    The `copilot` extra is ready as soon as you sign in (step 5). The **PII guard
    and name detection stay off until you set them in config** — installing
    `pii-spacy` only makes them *available*. See
    [step 5](#5-ask-the-ai-safely) and
    [Turn on the PII guard](ai-copilot.md#turn-on-the-pii-guard) for the exact
    settings.

That's the whole install. (Got a frozen `.pyz`/`.exe` from your admin instead?
See [Got a frozen build from your admin?](index.md#got-a-frozen-build-from-your-admin).)

## 2. Connect to your team

Run the app — your browser opens the **hub** at a local address. Your admin gives
you four values (`client_id`, `owner`, `repo`, `branch`); paste them into the
hub's setup card once (or your admin baked them in, and there's nothing to fill).
Then **Log in with GitHub**: the hub shows a short device code, you approve it in
your browser, and you're in — **no personal access token to create, paste, or
rotate**.

[More on first run and GitHub Enterprise →](index.md)

## 3. Pick up a teammate's notebook

The hub lists every notebook in the team repo with its sync status.

1. **Pull** to download the team's latest.
2. **Open** any notebook — it launches in the bundled marimo editor, running in
   the **same locked environment** your teammate used, so `import polars` just
   works.

That's the whole "pick it up and go" loop: no clone, no virtualenv, no "works on
my machine".

## 4. Share your work back

Edit in the marimo editor, then back in the hub:

- **Push** commits your change straight to the shared branch — one commit per
  file. If a teammate changed the same file first, GitHub rejects the stale write
  and the hub flags it for [per-file resolution](conflicts.md) — your work is
  never silently overwritten.
- **Propose** instead sends your changes to a personal review branch so they can
  land via a pull request (the only way in on a protected branch).

[The full daily workflow →](daily-workflow.md)

## 5. Ask the AI safely

With the `copilot` extra installed (step 1) and your org's Copilot policy enabled,
sign in to Copilot — the hub's **🤖 Copilot** menu, or `mooring ai login` — and the
**AI** button opens a chat beside any notebook. The copilot is **on by default**;
there's no setting to flip for the copilot itself.

It is **schema-only**: it sees your columns' names and types and your notebook's
code, but **never the data itself** — no values, no cell outputs, no data-file
contents. It *proposes* cells; you review the diff and Apply, and any Apply can be
rolled back.

!!! note "The PII safety nets are opt-in — turn them on in config"

    The copilot works as soon as it's installed, but its **best-effort PII / name
    scanning is off by default** (installing `pii-spacy` only makes it available).
    To switch it on:

    ```bash
    mooring config set ai.pii.enabled true       # structured-PII pre-flight scan
    mooring config set ai.pii.detect_names true  # + local name detection (needs the pii or pii-spacy extra)
    mooring ai pii doctor                         # confirm the name backend is ready
    ```

    With the `pii-spacy` extra the offline backend is selected automatically — no
    other setting needed. Full details (and the team data-dictionary option) are in
    [Turn on the PII guard](ai-copilot.md#turn-on-the-pii-guard).

[Why the copilot can't see your data →](../admins/ai-privacy.md)

## Where next

- [Daily workflow](daily-workflow.md) — every hub action, in depth.
- [What the copilot can do](ai-copilot.md) — enabling and using the AI.
- [When two people edit at once](conflicts.md) — resolving conflicts.
- [Command-line reference](cli.md) — everything the hub does, from a terminal.
