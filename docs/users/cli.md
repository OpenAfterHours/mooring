---
icon: lucide/terminal
---

# Command-line reference

Everything the hub does is also available from a terminal. Run the app file with
a command after it. The examples use `mooring.pyz`; with the Windows build, use
`mooring.exe` instead.

Running the app with **no command** opens the browser hub.

## Commands

```
python mooring.pyz hub [--no-browser] [--port PORT]
python mooring.pyz login
python mooring.pyz logout
python mooring.pyz whoami
python mooring.pyz status
python mooring.pyz pull [--theirs | --keep-both]
python mooring.pyz push [paths...] [-m "message"]
python mooring.pyz open notebooks/sales.py
python mooring.pyz new sales-analysis
python mooring.pyz selftest
python mooring.pyz version
```

### `hub`

Start the local browser hub (the default when you run with no command).

- `--no-browser` — start the server but don't open a browser tab.
- `--port PORT` — bind to a fixed port instead of a random free one. The hub
  always listens on `127.0.0.1` (localhost only).

### `login` / `logout` / `whoami`

- `login` — start GitHub device-flow login: open the printed URL, enter the
  code, and the token is saved to your OS credential store.
- `logout` — forget the stored token.
- `whoami` — print the logged-in GitHub username.

### `status`

List every workspace file with its sync state (unchanged, modified, remote,
conflicted, …) and a summary line.

### `pull`

Download changes from the team repo. Conflicts are handled by strategy:

- *(no flag)* — apply safe changes and **skip** conflicted files for manual
  resolution.
- `--theirs` — overwrite local edits with the remote versions.
- `--keep-both` — keep local edits and save remote versions as copies.

`--theirs` and `--keep-both` are mutually exclusive. See
[Resolving conflicts](conflicts.md).

### `push`

Upload local changes — one commit per file.

- `push` with no paths pushes **all** changed files.
- `push notebooks/a.py notebooks/b.py` pushes only those paths.
- `-m "message"` / `--message "message"` sets the commit message.

Files in conflict are blocked until resolved.

### `open` / `new`

- `open <workspace-relative-path>` — open an existing notebook in the marimo
  editor (e.g. `open notebooks/sales.py`).
- `new <name>` — create a notebook from the template and open it (e.g.
  `new sales-analysis`).

### `selftest`

Verify the bundled environment: checks each frozen package imports, prints your
config-file / workspace / log locations, the active `PYTHONPATH`, and whether a
team repo is configured. Useful for diagnosing a machine.

### `version`

Print the mooring version. `--version` (as a flag, e.g.
`python mooring.pyz --version`) does the same.
