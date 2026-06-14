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
python mooring.pyz login [--host HOST]
python mooring.pyz logout
python mooring.pyz whoami
python mooring.pyz repo list
python mooring.pyz repo add owner/name [--alias NAME] [--branch BR]
                            [--workspace PATH] [--host HOST] [--no-use]
python mooring.pyz repo use <alias>
python mooring.pyz repo remove <alias> | repo remove --all
python mooring.pyz status [--repo ALIAS]
python mooring.pyz pull [--theirs | --keep-both] [--repo ALIAS]
python mooring.pyz push [paths...] [-m "message"] [--repo ALIAS]
python mooring.pyz propose [paths...] [-m "message"] [--repo ALIAS]
python mooring.pyz open notebooks/sales.py
python mooring.pyz open reports/sales.pbip
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
    - `--host HOST` — log in to a **GitHub Enterprise** instance instead of
      public github.com, e.g. `login --host ghe.example.com` (a full URL like
      `https://ghe.example.com/` works too). The host is saved as the global
      host — it applies to *every* registered repo — and the device-flow login
      then runs against it. Omit the flag for github.com. Tokens are stored
      **per host**, so after switching hosts you log in again. See
      [GitHub Enterprise](../admins/github-setup.md#github-enterprise).
- `logout` — forget the stored token.
- `whoami` — print the logged-in GitHub username.

### `repo`

Manage the registered team repos (see
[Daily workflow → Switching repos](daily-workflow.md#switching-repos)):

- `repo list` — show every registered repo; `*` marks the active one.
- `repo add owner/name` — register a repo and switch to it. Options:
  `--alias NAME` (short name, defaults to the repo name), `--branch BRANCH`,
  `--workspace PATH` (custom local folder),
  `--host HOST` (a [GitHub Enterprise](../admins/github-setup.md#github-enterprise)
  host, saved as the global host — one host per installation),
  `--no-use` (register without switching).
- `repo use <alias>` — switch the active repo.
- `repo remove <alias>` — forget a repo. Its local workspace folder is kept;
  delete it manually if you no longer want the files. Use `repo remove --all`
  to forget every registered repo at once.

`status`, `pull`, `push`, `propose`, `open`, and `new` accept `--repo ALIAS`
to act on a registered repo **without** switching the active one.

### `status`

List every workspace file with its sync state (unchanged, modified, remote,
conflicted, …) and a summary line. Power BI project files are listed
individually here; the hub groups them per project.

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

### `propose`

Like `push`, but uploads to a personal **review branch** instead of the shared
branch, so the changes can be reviewed as a pull request (see
[Daily workflow → Proposing changes](daily-workflow.md#proposing-changes-for-review)):

- `propose` with no paths proposes **all** changed files;
  `propose notebooks/a.py` proposes only that path. `-m` sets the commit
  message, as with `push`.
- The output ends with a `.../compare/...` link on your GitHub host — open it
  to create the pull request. Mooring never creates the PR itself.
- Repeating `propose` while the pull request is open updates the same branch;
  once it merges (or its branch is deleted), the next `propose` starts a
  fresh one.
- Proposed files show as *in review* in `status` and are skipped by a plain
  `push`.

### `open` / `new`

- `open <workspace-relative-path>` — open an existing notebook in the marimo
  editor (e.g. `open notebooks/sales.py`). A `.pbip` path opens the project in
  **Power BI Desktop** instead (e.g. `open reports/sales.pbip`) — see
  [Power BI reports](power-bi.md).
- `new <name>` — create a notebook from the template and open it (e.g.
  `new sales-analysis`).

### `selftest`

Verify the bundled environment and print a diagnostic snapshot of the machine:

- each frozen package and its version (a `FAIL` line if one won't import),
- your config-file / workspace / log locations,
- the active `PYTHONPATH`,
- the **TLS trust** mode — OS trust store, or disabled via `MOORING_TRUSTSTORE=0`
  (see [Corporate networks & TLS](../admins/configuration.md#corporate-networks-tls)),
- the central-**logging** destination, or `off` when none is configured
  (see [Central logging](../admins/configuration.md#central-logging)),
- the configured **team repo**, with its branch and host.

Useful for diagnosing a machine — share the output with your admin when login or
sync misbehaves.

### `version`

Print the mooring version. `--version` (as a flag, e.g.
`python mooring.pyz --version`) does the same.
