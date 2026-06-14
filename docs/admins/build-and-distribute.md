---
icon: lucide/package
---

# Build & distribute

Mooring reaches your team two ways: installed from **PyPI** with `uvx mooring`
(on any Python 3.12+), or shipped as a single self-contained artifact built with
[moonlit](https://github.com/openafterhours/moonlit) for analysts with no Python
tooling. This page covers both — plus baking your config, building the artifacts,
the release workflow, changing the bundled packages, and getting the app to your
team.

!!! tip "Install from PyPI (no build step)"

    Anyone with **Python 3.12 or newer** can run Mooring without a frozen build —
    they use their own interpreter, so they pick the version:

    ```bash
    uvx mooring                 # run it directly, or:
    pip install mooring && mooring
    ```

    On first launch they fill in the
    [runtime setup form](configuration.md#the-runtime-setup-form) (or you bake
    config and publish your own build). The rest of this page is about the frozen
    `.pyz`/`.exe` for machines with **no** Python tooling at all.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed.
- Your four GitHub values from [GitHub setup](github-setup.md).

!!! note "Why moonlit is invoked with `uvx`"

    moonlit (the `.pyz`/`.exe` builder) is a build tool, not a project
    dependency, so it's run in an isolated environment via `uvx` rather than
    added to the lock. moonlit needs Python ≥ 3.13 to *run*; the app itself
    supports 3.12+. `--python-version 3.13` tells moonlit which runtime to build
    the artifact *for* (pick any 3.12+).

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

# zipapp — needs Python 3.13 on the user's machine
uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring.pyz --python-version 3.13

# Windows .exe — needs Python 3.13 on the user's machine
uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring.exe --windows-exe --python-version 3.13
```

For machines with **no Python at all**, build a folder bundle with embedded
CPython instead:

```bash
uvx --python 3.13 moonlit build -e mooring.cli:main -o dist/mooring-bundle --bundle-python --python-version 3.13
```

!!! warning "A frozen build targets one exact Python minor"

    A `.pyz`/`.exe` built for 3.13 requires the recipient to have Python
    **3.13.x** — an exact `major.minor` match, not a minimum. It won't run on
    3.12 or 3.14: moonlit's bootstrap (stamped into the artifact) exits with a
    clear error, and the bundled native wheels (`rpds_py`, `msgspec`, `loro`) are
    `cp313`-only and can't import on another minor anyway. This is a property of
    *freezing*, not of Mooring — the PyPI install above has no such constraint (it
    runs on any 3.12+). The `--bundle-python` build sidesteps it by embedding the
    interpreter (at the cost of a larger download).

    To build for a different minor, just change `--python-version` (e.g.
    `--python-version 3.12`). Because the project supports `>=3.12` and `uv.lock`
    is resolved across that whole range, **no pin or lock edits are needed** — any
    3.12+ target resolves from the same lock. (Dropping below 3.12 would need a
    `requires-python` change; the source needs ≥ 3.11 for `tomllib`.)

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
