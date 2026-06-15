---
icon: lucide/play
---

# Install & first run

This page gets you from nothing to a running hub. You only need **Python 3.13**
and the `mooring` app file your admin gave you — no git, no pip, no admin
rights.

## 1. Install Python 3.13

Each Mooring build targets one exact Python minor version, fixed when the app is
built — the builds you'll receive target **3.13.x**. Any 3.13 patch release works,
but a newer or older minor (e.g. 3.12 or 3.14) won't run a 3.13 build and will
exit with a clear message telling you which version to install.

=== "Windows"

    1. Download Python 3.13 from
       [python.org/downloads](https://www.python.org/downloads/).
    2. Run the installer and **tick *“Add python.exe to PATH”*** on the first
       screen.
    3. Confirm it worked — open a new terminal and run:

        ```powershell
        python --version
        # Python 3.13.x
        ```

=== "macOS"

    ```bash
    # with Homebrew
    brew install python@3.13
    python3.13 --version
    ```

=== "Linux"

    Use your distro's package (e.g. `apt install python3.13`) or
    [pyenv](https://github.com/pyenv/pyenv). Confirm with:

    ```bash
    python3.13 --version
    ```

## 2. Get the app

Your admin distributes one of these — put it anywhere, e.g. your Desktop:

| File | Run it with | Needs Python? |
|------|-------------|---------------|
| `mooring.exe` | double-click, or `mooring.exe` | Yes (3.13) |
| `mooring.pyz` | `python mooring.pyz` | Yes (3.13) |
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

!!! note "On GitHub Enterprise?"

    If your team uses a GitHub Enterprise instance rather than public
    github.com, the hub's setup card has a **GitHub URL** field for it (asked
    once, on first setup). From a terminal, point mooring at your instance with
    `mooring login --host ghe.example.com` before logging in (a full URL works
    too). Tokens are kept per host, so this is also how you switch instances.
    See [GitHub Enterprise](../admins/github-setup.md#github-enterprise) and the
    [`login` reference](cli.md#login-logout-whoami).

!!! tip "Installed with `uvx mooring`?"

    If you run Mooring from PyPI (`uvx mooring`) rather than a frozen app file,
    you can pull in an extra library just for that session — e.g. pandas
    alongside the bundled polars:

    ```bash
    uvx --with pandas mooring        # notebooks can now `import pandas`
    ```

    Repeat `--with` for several packages. See
    [Build & distribute](../admins/build-and-distribute.md) for the details.
    (Frozen `.exe`/`.pyz`/bundle files keep their fixed package set.)

## Next steps

- [Daily workflow](daily-workflow.md) — pull, open, edit, push, and create
  notebooks.
- [Resolving conflicts](conflicts.md) — what to do when two people edit the
  same file.
- [Command-line reference](cli.md) — everything the hub does, from a terminal.
