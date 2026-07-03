---
icon: lucide/terminal
---

# Command-line reference

Everything the hub does is also available from a terminal — including the
schema-only [AI copilot](ai-copilot.md), which sees your column names and types
and your notebook's code, but never the data itself.

The examples use bare `mooring <cmd>` — you installed it with `uvx mooring`,
`uv tool install mooring`, or `pip install mooring`. For a one-off run without
installing, prefix with `uvx`, e.g. `uvx mooring status`.

!!! note "Running a frozen .pyz/.exe build?"
    Use `python mooring.pyz <cmd>` (or `mooring.exe <cmd>`) instead of the bare
    `mooring <cmd>` shown below.

Running the app with **no command** opens the browser hub.

## Commands

```
mooring hub [--no-browser] [--port PORT]
mooring login [--host HOST]
mooring logout
mooring whoami
mooring repo list
mooring repo add owner/name [--alias NAME] [--branch BR]
                 [--workspace PATH] [--host HOST] [--no-use]
mooring repo use <alias>
mooring repo remove <alias> | repo remove --all
mooring status [--repo ALIAS]
mooring pull [--theirs | --keep-both] [--repo ALIAS]
mooring push [paths...] [-m "message"] [--repo ALIAS]
mooring propose [paths...] [-m "message"] [--repo ALIAS]
mooring open notebooks/sales.py
mooring open reports/sales.pbip
mooring new sales-analysis
mooring delete notebooks/sales.py [-y]
mooring rollback notebooks/sales.py [-y] [--conflicts]
mooring init
mooring deps add polars "scipy>=1.11"
mooring deps remove polars
mooring deps list
mooring deps lock
mooring build-requirements [-o FILE]
mooring ai status
mooring ai login [--host HOST]
mooring ai dictionary check [--repo ALIAS]
mooring ai pii check [--repo ALIAS] [--notebook REL]
mooring ai pii model [--repo ALIAS]
mooring ai pii doctor
mooring config set <key> <value...> | config get <key>
mooring config unset <key> | config list | config path
mooring selftest
mooring version
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

`status`, `pull`, `push`, `propose`, `open`, `new`, `delete`, and `rollback`
accept `--repo ALIAS` to act on a registered repo **without** switching the
active one.

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
  `new sales-analysis`). Pass a path to place it in a sub-folder (e.g.
  `new packages/finance/notebooks/sales`); mooring registers that folder so it
  syncs for the team. A bare name goes in `notebooks/`.

### `deliver` / `verify` / `checks`

- `deliver <workspace-relative-path>` — render a notebook to a self-contained
  HTML snapshot (code hidden) in the local `.mooring/outbox/` and print its path
  (e.g. `deliver notebooks/sales.py`). The notebook runs on your machine; the
  HTML embeds the values but lives in `.mooring`, which never syncs — attach it to
  email/Teams yourself. See [Delivering a result](daily-workflow.md#delivering-a-result-for-a-stakeholder).
- `verify <workspace-relative-path>` — smoke-run a notebook once on your machine and
  print whether it ran clean; exits non-zero if a cell failed. Records a value-free
  trust receipt (a boolean, never a value) that badges the notebook's row in the hub
  and clears itself when you edit the file. See
  [Verifying a notebook runs](daily-workflow.md#verifying-a-notebook-runs).
- `checks` — list the tie-out / data-quality check results recorded per notebook
  by `import mooring_checks` calls (value-free: names and pass/fail counts only).
  See [Checking your numbers tie out](daily-workflow.md#checking-your-numbers-tie-out).

### `init` / `deps` — notebook dependencies

A repo declares the packages its notebooks need in a `pyproject.toml` + `uv.lock`
at the workspace root, version-controlled alongside the notebooks. With uv on your
machine, mooring opens notebooks in that locked environment automatically; on a
frozen `.pyz` with no uv, the bundle the admin built is used and `open` warns if a
declared package isn't in it.

- `init` — scaffold the repo's `pyproject.toml` (seeded with just `marimo`) and,
  if uv is available, its `uv.lock`. `new` does this for you on the first
  notebook. Safe to re-run; it never overwrites an existing file.
- `deps add <pkg>…` — add packages and re-lock (e.g.
  `deps add polars "scipy>=1.11"`). Run `push` afterwards to share them.
- `deps remove <pkg>…` — remove packages and re-lock.
- `deps list` — show declared packages and whether each is available in the
  current environment.
- `deps lock` — refresh `uv.lock` from `pyproject.toml`.

`deps add`/`remove`/`lock` need [uv](https://docs.astral.sh/uv/) installed.

### `build-requirements`

Export the repo's declared packages (one per line, `marimo` omitted) for an admin
building a frozen artifact from this repo's stack — see
[Build & distribute → §4](../admins/build-and-distribute.md#changing-the-bundled-package-stack).
`-o FILE` writes to a file instead of standard output.

### `delete`

Delete a notebook from your workspace. A `.pbip` path removes the whole Power BI
project (its pointer plus the `.SemanticModel/` and `.Report/` folders).

```
mooring delete notebooks/sales.py
```

Deletion is **local only**: it removes the file(s) from your workspace, and the
notebook then shows as *deleted locally* in `status`. Run `push` (or `propose`)
afterwards to remove it from the team repo for everyone; a notebook you never
shared just disappears. You are asked to confirm first — pass `-y`/`--yes` to
skip the prompt (required when running non-interactively, e.g. from a script).

### `rollback`

Discard your local changes to a notebook and restore the last version you pulled
or pushed — go back to the last synced checkpoint.

```
mooring rollback notebooks/sales.py
```

- Works on a file that is *modified* or *deleted locally*. A never-synced file
  has no earlier version, so use [`delete`](#delete) for that instead.
- Unlike `delete`, the last-synced bytes come from the team repo, so rollback
  needs you to be **logged in**. It only ever changes your local file — never the
  team repo, and never a teammate's work.
- Your current version is snapshotted first (recoverable from the hub's **Undo**),
  so the rollback can be undone.
- You are asked to confirm — pass `-y`/`--yes` to skip the prompt (required when
  non-interactive). For a file in conflict, `pull` first, or pass `--conflicts`
  to drop your side and turn it into a clean pull.

### `ai`

The copilot's command family. The copilot is **opt-in** and needs the `copilot`
extra plus an in-app Copilot sign-in — see [AI copilot](ai-copilot.md) and
[why the copilot can't see your data](../admins/ai-privacy.md).

- `ai status` — show the AI provider's sign-in status.
- `ai login [--host HOST]` — sign in to GitHub Copilot via OAuth device flow.
  This is **separate from your mooring GitHub login** (it can be a different
  account); `--host` targets a GitHub Enterprise instance for data residency.
- `ai dictionary check [--repo ALIAS]` — parse the team data dictionary under
  `context/` and report the tables, columns, and any keys dropped by the
  five-slot allowlist or the secret/PII scan.
- `ai pii check [--repo ALIAS] [--notebook REL]` — offline scan of
  `instructions.md`, the dictionaries, and (with `--notebook`) a single notebook
  for structured-PII shapes. Findings are value-free (a line and a kind).
- `ai pii model [--repo ALIAS]` — download or verify the local NER
  name-detection model (needs the `pii`, or `pii-spacy` for the offline backend,
  extra).
- `ai pii doctor` — check the PII guard end-to-end: which backend runs, what's
  ready, and what to fix.

### `config`

Read and edit your user `config.toml` by **dotted key**, without hand-editing the
file (every other setting is preserved):

```
mooring config set ai.pii.enabled true
mooring config set ai.pii.name_labels person name organization
mooring config get ai.pii.enabled        # effective value
mooring config unset ai.pii.enabled      # revert to the default
mooring config list                      # whole effective config
mooring config path                      # config.toml location
```

`true`/`false` become booleans and numbers are parsed; several tokens become a
list; anything else stays a string. See
[Configuration → editing from the command line](../admins/configuration.md#editing-the-user-config-from-the-command-line).

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
`mooring --version`) does the same.
