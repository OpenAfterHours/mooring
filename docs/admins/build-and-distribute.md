---
icon: lucide/package
---

# Build & distribute

Mooring reaches your team two ways: installed from **PyPI** with `uvx mooring`
(on any Python 3.12+), or shipped as a single self-contained artifact built with
[moonlit](https://github.com/openafterhours/moonlit) for analysts with no Python
tooling. This page covers both — plus baking your config, building the artifacts,
the release workflow, building a repo's package stack into a frozen artifact, and
getting the app to your team.

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

!!! tip "Notebook dependencies live in the repo (PyPI/uvx)"

    A repo declares the packages its notebooks need in a `pyproject.toml` +
    `uv.lock` at its root, version-controlled alongside the notebooks. Mooring
    itself ships lean (just its own runtime) — the team chooses the stack:

    ```bash
    mooring init                              # scaffold pyproject.toml (marimo) + uv.lock
    mooring deps add polars duckdb "scipy>=1.11"
    mooring push                              # share the change with the team
    ```

    With uv available, mooring opens each notebook in that locked environment
    (`uv run --frozen marimo edit …`), so everyone gets identical versions and
    `import polars` just works. For a one-off package you don't want to commit,
    uv's `--with` still injects extras into a single run:

    ```bash
    uvx --with pandas mooring                 # ad-hoc, not recorded in the repo
    ```

    Frozen `.pyz`/`.exe` artifacts have no uv at runtime — their importable set is
    whatever the admin built in (see [§4](#changing-the-bundled-package-stack)).
    Opening a notebook that needs a package the bundle lacks shows a warning.

## Optional extras

Mooring ships **lean** — the base install is just its own runtime. Three opt-in
features live behind *extras*, so their heavy dependencies stay out of the
default install (and out of the frozen `.pyz`):

| Extra | Adds | Enables |
|-------|------|---------|
| `copilot` | `github-copilot-sdk` (fetches a native CLI on first use) | the [AI copilot](../users/ai-copilot.md) |
| `pii` | `gliner` (torch + transformers) | [NER name detection](ai-privacy.md#name-detection-opt-in-local-ner) for the PII guard |
| `pii-spacy` | `spacy` + the bundled `mooring-spacy-en-md` model | [offline name detection](ai-privacy.md#spacy-backend) for air-gapped teams |

Install the extra with whichever uv idiom matches how you run mooring. **Quote
the brackets** — `[...]` is a glob in zsh and bash and is special to PowerShell,
so an unquoted `mooring[copilot]` can silently expand to nothing:

```bash
uvx "mooring[copilot]"               # run as a one-off tool (nothing stays installed)
uv tool install "mooring[copilot]"   # install mooring as a persistent CLI tool
uv add "mooring[copilot]"            # add mooring to your own uv project
pip install "mooring[copilot]"       # plain pip, into the active environment
```

Combine extras with a comma — `"mooring[copilot,pii]"`. The base package is
identical regardless; an extra only pulls in its own extra dependencies. To drop
back to lean, reinstall (or `uv add`) without the brackets.

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

## 4. Build a frozen artifact for a repo's stack { #changing-the-bundled-package-stack }

Frozen `.pyz`/`.exe` artifacts have no pip/uv at runtime, so a notebook can only
import what's bundled. Mooring itself ships **lean** — only its own runtime — so
the notebook packages come from **the repo's** `pyproject.toml` (the same file uv
users run against). Generate the bundle from it so the two delivery modes never
drift:

1. In a synced workspace for that repo, export its declared packages:

   ```bash
   mooring build-requirements -o build-reqs.txt
   ```

2. In your mooring build checkout, add those packages and re-sync the lock:

   ```bash
   uv add -r build-reqs.txt
   uv sync
   ```

3. Rebuild the artifacts (step 2 above) and redistribute. The `.pyz` now bundles
   mooring **plus** the repo's packages, so its notebooks import them with no uv.

!!! note "Drive the bundle from the repo, not by hand"

    Don't commit analyst packages into mooring's own `pyproject.toml` — that
    re-introduces the bloat this design removed and drifts from the repo. Add them
    in a throwaway, team-specific build checkout via `mooring build-requirements`
    so the repo stays the single source of truth.

!!! note "PyPI/uvx users don't need a rebuild"

    With uv, notebooks run against the repo's `pyproject.toml` / `uv.lock`
    directly. A frozen rebuild is only for machines with no Python tooling.

## 5. Distribute { #distribute }

Hand the artifact to your analysts any way you like:

- A file share or network drive.
- Email (size depends on the repo's packages — see the note below).
- A **GitHub Release** (automatic with the `v*` tag workflow above).

Then point them at [Install & first run](../users/index.md). If you baked the
config, they just run it and log in; if not, they'll complete the
[runtime setup form](configuration.md#the-runtime-setup-form) once.

!!! note "Artifact size & first run"

    Size tracks what you bundle: a lean marimo-only build is the floor; each
    package the repo adds (§4) grows it (a typical analyst stack lands around
    ~110 MB). First run extracts to a local cache (`%LOCALAPPDATA%\moonlit\` on
    Windows); old versions' caches can be deleted freely.
