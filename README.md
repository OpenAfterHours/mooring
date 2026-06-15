# ⚓ mooring

Git-free [marimo](https://marimo.io) notebook sharing via GitHub.

Mooring is a single-file app (`mooring.pyz` / `mooring.exe`) that lets a team
of data analysts pull, edit, and push marimo notebooks stored in a shared
GitHub repo — **without git installed on their machines**. All sync happens
over the GitHub REST API; the only requirement on an analyst's machine is
Python 3.12 or newer.

Double-clicking the app opens a local browser **hub**: log in to GitHub with
a one-time device code, see every team notebook with its sync status, pull
the latest, open notebooks in the bundled marimo editor, and push your
changes back — one commit per file, with conflicts detected and resolved
per file (never silently overwritten).

## How it works

- **One shared team repo** (e.g. `your-org/notebooks`) holds `notebooks/`,
  `data/`, and `reports/` folders. Everyone pulls from and pushes to it.
- **No git anywhere.** Pull walks the repo tree via the GitHub Git Data API
  and downloads only changed blobs; push uses the Contents API with the
  file's last-known SHA, so GitHub itself rejects writes that would clobber
  someone else's change.
- **Conflicts are explicit.** Pull never overwrites local edits; push blocks
  conflicted files, offering per-file resolution.
- **Push or propose.** Push commits straight to the shared branch; **propose**
  sends changes to a personal review branch so they can land via a pull
  request — protect the branch and propose becomes the only way in.
- **Dependencies live with the repo.** A repo declares its notebook packages in
  a `pyproject.toml` + `uv.lock` at its root (run `mooring init`, then
  `mooring deps add <pkg>`), version-controlled alongside the notebooks. With uv,
  notebooks run in that locked environment automatically; mooring itself ships
  lean (no opinionated analyst stack baked in). For machines with no uv, an admin
  builds a frozen `.pyz` whose bundle is generated from that same `pyproject.toml`
  — one source of truth, two delivery modes (see
  [build & distribute](docs/admins/build-and-distribute.md)).
- **Works on corporate GitHub.** GitHub Enterprise instances are supported
  (`mooring login --host ghe.example.com`), and TLS is verified against the
  OS trust store, so SSL-intercepting proxies with an IT-installed root CA
  just work.

## Documentation

Full docs live in [`docs/`](docs/) and build into a searchable site with
[zensical](https://zensical.org):

- **[For users](docs/users/index.md)** — install Python, run the app, and
  pull / edit / push notebooks ([daily workflow](docs/users/daily-workflow.md),
  [conflicts](docs/users/conflicts.md), [CLI](docs/users/cli.md)).
- **[For admins](docs/admins/index.md)** — set up a team:
  [GitHub setup](docs/admins/github-setup.md) (the repo, OAuth app, client id,
  scopes), [configuration](docs/admins/configuration.md), and
  [build & distribute](docs/admins/build-and-distribute.md).
- **[For developers](docs/developers/index.md)** —
  [architecture](docs/developers/index.md) and
  [contributing](docs/developers/contributing.md).

### Build & preview the docs

```
uv sync
uv run zensical serve     # live-reloading preview at a local URL
uv run zensical build     # static site into ./site
```

`.github/workflows/docs.yml` publishes the site to GitHub Pages on every push
to the default branch.

## Develop

```
uv sync                                  # install everything
uv run pytest                            # unit tests (no network needed)
uv run ruff check src tests              # lint
uv run mooring hub                       # run the hub from source
```

See [contributing](docs/developers/contributing.md) for the architecture,
integration testing, and project conventions.

---

Built with [moonlit](https://github.com/openafterhours/moonlit).
