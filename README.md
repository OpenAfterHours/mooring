# ⚓ mooring

Git-free [marimo](https://marimo.io) notebook sharing via GitHub.

Mooring is a single-file app (`mooring.pyz` / `mooring.exe`) that lets a team
of data analysts pull, edit, and push marimo notebooks stored in a shared
GitHub repo — **without git installed on their machines**. All sync happens
over the GitHub REST API; the only requirement on an analyst's machine is
Python 3.12.

Double-clicking the app opens a local browser **hub**: log in to GitHub with
a one-time device code, see every team notebook with its sync status, pull
the latest, open notebooks in the bundled marimo editor, and push your
changes back — one commit per file, with conflicts detected and resolved
per file (never silently overwritten).

Built with [moonlit](https://github.com/openafterhours/moonlit).

---

## How it works

- **One shared team repo** (e.g. `your-org/notebooks`) holds `notebooks/`
  and `data/` folders. Everyone pulls from and pushes to it.
- **No git anywhere.** Pull walks the repo tree via the GitHub Git Data API
  and downloads only changed blobs; push uses the Contents API with the
  file's last-known SHA, so GitHub itself rejects writes that would clobber
  someone else's change.
- **Three-way change detection.** Mooring computes git blob SHAs locally and
  keeps a manifest of what was last synced, so it always knows whether a file
  is modified locally, changed remotely, or conflicted.
- **Conflicts are explicit.** Pull never overwrites local edits; push blocks
  conflicted files. The hub offers per-file resolution: *Use remote*,
  *Keep both*, or *Push as copy* (publishes your version under
  `name-<your-github-login>.py`).
- **Frozen package stack.** Notebooks can import anything bundled into the
  artifact: `polars`, `altair`, `plotly`, `openpyxl`, `fastexcel`,
  `requests` (plus the standard library). There is no pip at runtime.

## For analysts: install & use

1. Install [Python 3.12](https://www.python.org/downloads/) (tick
   *"Add python.exe to PATH"*).
2. Get `mooring.exe` (or `mooring.pyz`) from your admin and put it anywhere,
   e.g. your Desktop.
3. Run it (`mooring.exe`, or `python mooring.pyz`). Your browser opens the hub.
4. Click **Log in with GitHub**, enter the code shown at
   [github.com/login/device](https://github.com/login/device).
5. **Pull** to fetch the team's notebooks, **Open** to edit one in marimo,
   **New notebook** to start fresh, **Push** to share your work.

Notebooks live in `Documents\mooring\<repo>\notebooks\`; data files your
notebooks read go in `...\<repo>\data\` and sync the same way.

The first launch takes a minute while the app unpacks itself to a local
cache; later launches are fast.

### CLI (optional)

Everything the hub does is also available from a terminal:

```
python mooring.pyz login | logout | whoami
python mooring.pyz status
python mooring.pyz pull [--theirs | --keep-both]
python mooring.pyz push [paths...] [-m "message"]
python mooring.pyz open notebooks/sales.py
python mooring.pyz new sales-analysis
python mooring.pyz selftest
```

## For admins: set up a team

1. **Create the shared repo**, e.g. `your-org/notebooks`, with empty
   `notebooks/` and `data/` folders. Don't enable git-LFS (the API would
   deliver pointer files).
2. **Register a GitHub OAuth app** (Settings → Developer settings → OAuth
   apps → New). Any homepage/callback URL works; then **enable Device Flow**
   on the app. Copy the client id — there is no secret to manage.
   - If the repo lives in an org with third-party-app restrictions, an org
     owner must approve the OAuth app.
3. **Bake the config**: edit `src/mooring/config_default.toml` with the
   client id, owner, repo, and branch.
4. **Build** (requires [uv](https://docs.astral.sh/uv/)):

   ```
   uv sync
   uv run pytest
   uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring.pyz --python-version 3.12
   uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring.exe --windows-exe --python-version 3.12
   ```

   For machines with **no Python at all**, build a folder bundle with
   embedded CPython instead:

   ```
   uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring-bundle --bundle-python --python-version 3.12
   ```

5. **Distribute** the artifact (file share, email, GitHub release — the
   `.github/workflows/release.yml` workflow builds and attaches artifacts on
   every `v*` tag).

Users without a baked config get a one-time setup form in the hub instead;
their values are stored in `%APPDATA%\mooring\config.toml`.

### Changing the bundled notebook packages

Edit `dependencies` in `pyproject.toml`, `uv sync`, rebuild, redistribute.
Notebooks can only import what's frozen into the artifact.

## Development

```
uv sync                                  # install everything
uv run pytest                            # unit tests (no network needed)
uv run ruff check src tests              # lint
uv run mooring hub                       # run the hub from source
uv run python tests/manual_editor_check.py   # editor-subprocess smoke test
```

Layout: `src/mooring/` — `cli.py` (entry point; also sets PYTHONPATH so the
marimo subprocess works from inside the packaged artifact), `auth.py` (device
flow + keyring), `github.py` (REST client), `gitsha.py`/`manifest.py`/`sync.py`
(the three-way sync engine), `editor.py` (marimo subprocess manager),
`hub/` (Starlette app + static frontend).

To integration-test sync against a real repo, set `MOORING_TOKEN` (a PAT
works) plus `MOORING_OWNER`/`MOORING_REPO`/`MOORING_CLIENT_ID` and use the
CLI against a scratch repository.

## Constraints & notes

- **Python version is pinned.** A `.pyz`/`.exe` built for 3.12 needs the
  user to have Python 3.12.x; moonlit shows a clear error otherwise. The
  `--bundle-python` build escapes this entirely.
- **File sizes**: pushes warn at 10 MB and refuse at 45 MB per file (GitHub
  Contents API limit). Keep big datasets out of the repo.
- **Tokens** are stored in the OS credential store (Windows Credential
  Manager); `repo`-scoped OAuth tokens grant access to all repos the user
  can reach — use a dedicated machine account org if that's a concern.
- **Artifact size** is ~110 MB (marimo + polars + plotly + altair). First
  run extracts to `%LOCALAPPDATA%\moonlit\`; old versions' caches can be
  deleted freely.
