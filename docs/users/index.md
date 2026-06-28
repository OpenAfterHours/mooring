---
icon: lucide/play
title: Install & sign in
---

# Install & sign in

This page gets you from nothing to a running hub. The simple path needs only
**Python 3.12+** and [uv](https://docs.astral.sh/uv/) — **no git, no tokens, no
admin rights**. (If your admin handed you a frozen `mooring.exe` / `.pyz` instead,
jump to [Got a frozen build from your admin?](#got-a-frozen-build-from-your-admin).)

## 1. Install

With **Python 3.12 or newer** and [uv](https://docs.astral.sh/uv/), run Mooring
straight from PyPI:

```bash
uvx mooring                  # run it as a one-off tool
uv tool install mooring      # …or install it as a persistent CLI
pip install mooring          # …or into the active environment, then `mooring`
```

Want the AI copilot — or its optional offline PII / name scanning? Add an extra
(**quote the brackets** so the shell doesn't glob them):

```bash
uv tool install "mooring[copilot]"            # + the schema-only AI copilot
uv tool install "mooring[copilot,pii-spacy]"  # + offline PII & name detection
```

!!! note "Installing an extra doesn't switch the feature on"

    The copilot is ready once you sign in; the **PII guard and name detection stay
    off until you set them in config**. See
    [What the copilot can do](ai-copilot.md) and
    [Turn on the PII guard](ai-copilot.md#turn-on-the-pii-guard).

No Python yet? Install it first, then come back to `uvx mooring`:

=== "Windows"

    1. Download Python from
       [python.org/downloads](https://www.python.org/downloads/) (3.12 or newer).
    2. Run the installer and **tick *“Add python.exe to PATH”*** on the first
       screen.
    3. Confirm — open a new terminal and run `python --version`.
    4. Install [uv](https://docs.astral.sh/uv/), then run `uvx mooring`.

=== "macOS"

    ```bash
    brew install python uv     # or see astral.sh/uv to install uv on its own
    uvx mooring
    ```

=== "Linux"

    Use your distro's Python (3.12+) or [pyenv](https://github.com/pyenv/pyenv),
    install [uv](https://docs.astral.sh/uv/), then run `uvx mooring`.

## 2. Open the hub and sign in

1. Run the app. Your browser opens the **hub** at a local address
   (`http://127.0.0.1:…`).
2. Click **Log in with GitHub**.
3. The hub shows a short code and opens your GitHub sign-in page
   (`github.com/login/device`, or your company's GitHub instance) — enter the
   code there and authorize.
4. Once authorized, the hub shows the team's notebooks and their sync status.

**No personal access token to wrangle.** Mooring uses GitHub's OAuth **Device
Flow**: the app only knows a public client id (no secret). You approve a code in
your browser and the app receives a token, which is stored in your OS credential
store (Windows Credential Manager / macOS Keychain) so you stay logged in between
runs. There's nothing to create, paste, or rotate by hand.

!!! note "On GitHub Enterprise?"

    If your team uses a GitHub Enterprise instance rather than public github.com,
    the hub's setup card has a **GitHub URL** field for it (asked once, on first
    setup). From a terminal, point mooring at your instance with
    `mooring login --host ghe.example.com` before logging in (a full URL works
    too). Tokens are kept per host, so this is also how you switch instances. See
    [GitHub Enterprise](../admins/github-setup.md#github-enterprise) and the
    [`login` reference](cli.md#login-logout-whoami).

!!! tip "Packages your notebooks need"

    A repo's notebook packages live in a `pyproject.toml` + `uv.lock` at its root,
    shared with the team through GitHub. Add to them with:

    ```bash
    mooring deps add polars "scipy>=1.11"   # then `mooring push` to share
    ```

    With uv, notebooks open in that locked environment automatically. For a
    one-off package you don't want to commit, `uvx --with pandas mooring` injects
    it for that session only. See the
    [CLI reference](cli.md#init-deps-notebook-dependencies).

## Got a frozen build from your admin?

Some teams ship a self-contained **`mooring.exe` / `mooring.pyz`** for machines
with no Python tooling at all. There's nothing to install with uv — but a frozen
build targets one exact Python minor version, fixed when it was built.

| File | Run it with | Needs Python? |
|------|-------------|---------------|
| `mooring.exe` | double-click, or `mooring.exe` | Yes — the exact minor it was built for |
| `mooring.pyz` | `python mooring.pyz` | Yes — the exact minor it was built for |
| `mooring-bundle/` folder | run the launcher inside | **No** — Python is embedded |

Your admin will tell you which Python version to install (e.g. 3.13.x); a
different minor won't run the build and it exits with a clear message telling you
which to install. First launch unpacks to a local cache and is slower; later
launches are fast. Everything else on this page — signing in, pulling, pushing —
is identical. See
[Advanced: offline / frozen builds](../admins/build-and-distribute.md) for how
these are made.

## Next steps

- [Quickstart](quickstart.md) — the whole journey in five minutes.
- [Daily workflow](daily-workflow.md) — pull, open, edit, push, and create
  notebooks.
- [What the copilot can do](ai-copilot.md) — an AI assistant that's sent only your
  schema and source, never your data values.
- [When two people edit at once](conflicts.md) — resolving conflicts.
- [Command-line reference](cli.md) — everything the hub does, from a terminal.
