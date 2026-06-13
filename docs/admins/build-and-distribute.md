---
icon: lucide/package
---

# Build & distribute

Mooring ships as a single self-contained artifact built with
[moonlit](https://github.com/openafterhours/moonlit). This page covers baking
your config, building the artifacts, the release workflow, changing the bundled
packages, and getting the app to your team.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed.
- Your four GitHub values from [GitHub setup](github-setup.md).

!!! note "Why moonlit is invoked with `uvx --python 3.13`"

    moonlit (the `.pyz`/`.exe` builder) needs Python ≥ 3.13, while the app
    itself targets your team's **3.12**. So moonlit is **not** a project
    dependency — it's run in an isolated environment via `uvx`, while
    `--python-version 3.12` tells it which runtime to build *for*.

## 1. Bake your config

Edit `src/mooring/config_default.toml` with your `client_id`, `owner`, `repo`,
and `branch` (and adjust `[sync]` limits if needed) so analysts get a
ready-to-use app. See [Configuration](configuration.md) for every key.

To watch usage and field errors across your team, set `[logging] endpoint` to a
collector URL or a shared folder/UNC path — see
[Central logging](configuration.md#central-logging). Leave it empty to disable.

## 2. Build the artifacts

```bash
uv sync
uv run pytest

# zipapp — needs Python 3.12 on the user's machine
uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring.pyz --python-version 3.12

# Windows .exe — needs Python 3.12 on the user's machine
uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring.exe --windows-exe --python-version 3.12
```

For machines with **no Python at all**, build a folder bundle with embedded
CPython instead:

```bash
uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring-bundle --bundle-python --python-version 3.12
```

!!! warning "Python version is pinned"

    A `.pyz`/`.exe` built for 3.12 requires the user to have Python **3.12.x**;
    moonlit shows a clear error otherwise. The `--bundle-python` build escapes
    this entirely by embedding the interpreter (at the cost of a larger
    download).

## 3. Release workflow (optional)

`.github/workflows/release.yml` automates the build on every `v*` tag: it runs
on Windows, syncs deps, lints (`ruff`), tests (`pytest`), builds
`dist/mooring.pyz` and `dist/mooring.exe`, runs a smoke test that strips `PATH`
down to Python only (proving the artifact works with **no git**), and uploads
the artifacts to a GitHub Release.

To cut a release: tag a commit `vX.Y.Z` and push the tag.

!!! note "Two separate workflows"

    `release.yml` builds the **app**. A second workflow,
    `.github/workflows/docs.yml`, builds and publishes **this documentation
    site** — see [Contributing](../developers/contributing.md#working-on-the-docs).

## 4. Changing the bundled package stack

Notebooks can only import what's frozen into the artifact (currently `polars`,
`altair`, `plotly`, `openpyxl`, `fastexcel`, `requests`, plus the standard
library — there is no pip at runtime). To add or remove one:

1. Edit `dependencies` in `pyproject.toml`.
2. `uv sync`.
3. Rebuild (step 2) and redistribute.

## 5. Distribute { #distribute }

Hand the artifact to your analysts any way you like:

- A file share or network drive.
- Email (note the ~110 MB size for the `.pyz`/`.exe`).
- A **GitHub Release** (automatic with the `v*` tag workflow above).

Then point them at [Install & first run](../users/index.md). If you baked the
config, they just run it and log in; if not, they'll complete the
[runtime setup form](configuration.md#the-runtime-setup-form) once.

!!! note "Artifact size & first run"

    The `.pyz`/`.exe` is ~110 MB (marimo + polars + plotly + altair). First run
    extracts to a local cache (`%LOCALAPPDATA%\moonlit\` on Windows); old
    versions' caches can be deleted freely.
