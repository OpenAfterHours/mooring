---
icon: lucide/git-pull-request
---

# Contributing

## Dev setup

You need [uv](https://docs.astral.sh/uv/). Then:

```bash
uv sync                                  # install everything (incl. dev deps)
uv run pytest                            # unit tests — no network needed
uv run ruff check src tests              # lint
uv run mooring hub                       # run the hub from source
uv run python tests/manual_editor_check.py   # editor-subprocess smoke test
```

The unit tests mock GitHub with `responses`, so they run offline. `ruff` is
configured with a line length of 100.

## Running from source

`uv run mooring <command>` runs the CLI exactly as the packaged app does — e.g.
`uv run mooring hub`, `uv run mooring status`. See the
[CLI reference](../users/cli.md) for all commands.

## Integration testing

To exercise the real sync engine against a live repo, point mooring at a scratch
repository with environment variables instead of logging in:

```bash
export MOORING_TOKEN="ghp_..."        # a PAT works; skips device flow
export MOORING_CLIENT_ID="Ov23li..."
export MOORING_OWNER="your-org"
export MOORING_REPO="scratch-notebooks"
uv run mooring status
uv run mooring pull
```

These `MOORING_*` variables override both config files for the run — see
[Configuration](../admins/configuration.md#environment-variables). Use a
throwaway repo; pushes create real commits.

## Gotchas worth knowing

- **PYTHONPATH for the marimo subprocess.** When packaged, moonlit activates its
  extracted site-packages via `site.addsitedir()`, which child processes don't
  inherit. `cli._ensure_child_pythonpath()` re-exposes them on `PYTHONPATH` so
  the marimo server and its kernels can import the bundled stack. Don't remove
  it.
- **UTF-8 BOM breaks notebooks.** marimo rejects `.py` notebooks that start with
  a UTF-8 BOM. PowerShell 5.1's `Out-File -Encoding utf8` writes one — use a
  BOM-less writer when generating notebook files.
- **`httpx` vs `httpx2`.** Starlette's test client now prefers `httpx2`; the
  project pins plain `httpx` (behind a deprecation warning) as the conservative
  choice. Swap when comfortable.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `master` and every pull
request: a single **test** job (Windows, matching the release target) running
`ruff` → `lint-imports` → `pytest` → the Node JS tests. Run the same locally
before pushing.

Static code-quality analysis is handled **outside** Actions by
[SonarQube Cloud](https://sonarcloud.io) **Automatic Analysis**: the SonarCloud
GitHub App scans every push and PR for bugs, vulnerabilities, and code smells
and posts a check on each PR. There is no scanner step in CI and no
`SONAR_TOKEN` — matching the rest of the org (e.g. `rwa_calculator`). The
analysis scope (sources, tests, Python version, exclusions) is set by
`sonar-project.properties` at the repo root.

!!! note "One-time SonarQube Cloud setup"

    A SonarCloud org admin connects the project once:

    1. At [sonarcloud.io](https://sonarcloud.io), sign in with GitHub and add
       the **`OpenAfterHours/mooring`** repository to the **openafterhours**
       organization (the SonarCloud project key is `OpenAfterHours_mooring`).
    2. Ensure the **SonarCloud GitHub App** is installed with access to this
       repository (GitHub → Org settings → GitHub Apps), so it can read the
       code and post PR checks.
    3. Leave **Automatic Analysis** enabled (project → Administration →
       Analysis Method). No `SONAR_TOKEN` secret and no Actions job are needed.

## Working on the docs

The documentation is a [zensical](https://zensical.org) site under `docs/`,
configured by `zensical.toml` at the repo root.

```bash
uv run zensical serve     # live-reloading preview at a local URL
uv run zensical build     # build the static site into ./site
```

`zensical` is already a dev dependency, so `uv sync` installs it. The pages use
admonitions (`!!! note`), collapsible blocks (`??? info`), content tabs
(`=== "Windows"`), task lists, and mermaid diagrams — all enabled in
`zensical.toml`.

### How the docs get published

`.github/workflows/docs.yml` builds the site and deploys it to **GitHub Pages**
on every push to the default branch (and on manual dispatch). Edits to `docs/**`
or `zensical.toml` go live automatically.

!!! note "One-time Pages setup"

    For the first deploy to publish, a repo admin must set
    **Settings → Pages → Source = GitHub Actions**. After that it's automatic.
    Consider also setting `site_url` in `zensical.toml` to your Pages URL so
    canonical links and the sitemap are correct.

## Cutting a release

`scripts/release.py` does the whole dance — bump, check, commit, tag, push:

```bash
uv run python scripts/release.py                  # patch: 0.1.0 -> 0.1.1
uv run python scripts/release.py minor            # 0.1.0 -> 0.2.0 (also: major)
uv run python scripts/release.py --version 1.0.0  # set an explicit version
uv run python scripts/release.py minor --dry-run  # preview without changing anything
```

It refuses to run unless you are on a clean, up-to-date `master`; then it bumps
the version in `pyproject.toml` + `uv.lock` (via `uv version`) and
`src/mooring/__init__.py`, runs lint and tests, commits `release: vX.Y.Z`,
tags `vX.Y.Z`, and pushes branch and tag together. It needs only Python 3.12+
with git and uv on `PATH` (no PowerShell), so it works the same on
Windows/macOS/Linux.

The pushed tag triggers `.github/workflows/release.yml`, which re-runs the
checks, builds `mooring.pyz` / `mooring.exe`, attaches them to a GitHub
Release, and publishes the sdist + wheel to PyPI. A guard step fails the
publish if the tag, `pyproject.toml`, and `__init__.py` versions disagree, so
hand-rolled tags can't ship a mislabeled package.

## Conventions

- Keep modules small and single-purpose, matching the existing
  [layout](index.md#code-layout).
- Match surrounding style; run `ruff` before pushing.
- Add or update tests under `tests/` for behavior changes — they should stay
  offline (mock GitHub with `responses`).
