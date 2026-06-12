---
icon: lucide/play
---

# Install & first run

This page gets you from nothing to a running hub. You only need **Python 3.12**
and the `mooring` app file your admin gave you — no git, no pip, no admin
rights.

## 1. Install Python 3.12

Mooring is pinned to Python **3.12.x**. A newer or older Python won't run a
3.12 build (the app shows a clear error if so).

=== "Windows"

    1. Download Python 3.12 from
       [python.org/downloads](https://www.python.org/downloads/).
    2. Run the installer and **tick *“Add python.exe to PATH”*** on the first
       screen.
    3. Confirm it worked — open a new terminal and run:

        ```powershell
        python --version
        # Python 3.12.x
        ```

=== "macOS"

    ```bash
    # with Homebrew
    brew install python@3.12
    python3.12 --version
    ```

=== "Linux"

    Use your distro's package (e.g. `apt install python3.12`) or
    [pyenv](https://github.com/pyenv/pyenv). Confirm with:

    ```bash
    python3.12 --version
    ```

## 2. Get the app

Your admin distributes one of these — put it anywhere, e.g. your Desktop:

| File | Run it with | Needs Python? |
|------|-------------|---------------|
| `mooring.exe` | double-click, or `mooring.exe` | Yes (3.12) |
| `mooring.pyz` | `python mooring.pyz` | Yes (3.12) |
| `mooring-bundle/` folder | run the launcher inside | **No** — Python is embedded |

!!! note "First launch is slow"

    The first run unpacks the app (~110 MB: marimo + polars + plotly + altair)
    to a local cache. Later launches are fast.

## 3. Open the hub and log in

1. Run the app. Your browser opens the **hub** at a local address
   (`http://127.0.0.1:…`).
2. Click **Log in with GitHub**.
3. The hub shows a short code and opens your GitHub sign-in page
   (`github.com/login/device`, or your company's GitHub instance) — enter the
   code there and authorize.
4. Once authorized, the hub shows the team's notebooks and their sync status.

??? info "What's happening during login?"

    Mooring uses GitHub's **OAuth Device Flow**: the app only knows a public
    client id (no secret). It asks GitHub for a code, you approve it in your
    browser, and the app receives a token. The token is stored in your OS
    credential store (Windows Credential Manager / macOS Keychain), so you stay
    logged in between runs. See [GitHub setup](../admins/github-setup.md) for
    the admin side.

## Next steps

- [Daily workflow](daily-workflow.md) — pull, open, edit, push, and create
  notebooks.
- [Resolving conflicts](conflicts.md) — what to do when two people edit the
  same file.
- [Command-line reference](cli.md) — everything the hub does, from a terminal.
