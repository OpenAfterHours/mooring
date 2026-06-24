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
request. It has two jobs:

- **test** (Windows, matching the release target): `ruff` → `lint-imports` →
  `pytest` → the Node JS tests. Run the same locally before pushing.
- **SonarQube Cloud** (Linux): a static-analysis scan via
  [SonarQube Cloud](https://sonarcloud.io) for bugs, vulnerabilities, code
  smells, and duplication. Configured by `sonar-project.properties` at the repo
  root. It runs in parallel with `test` and reports a check on each PR. (The
  scanner is a Linux container action, which is why it can't share the Windows
  job.)

!!! note "One-time SonarQube Cloud setup"

    The scan needs a project and a token that only a repo admin can create:

    1. At [sonarcloud.io](https://sonarcloud.io), sign in with GitHub and
       **import the `OpenAfterHours/mooring` repository** (creating the
       organization first if needed).
    2. In the new project, open **Administration → Analysis Method** and
       **turn off "Automatic Analysis"** — it conflicts with, and will reject,
       the CI-based scan.
    3. Confirm the `sonar.organization` and `sonar.projectKey` in
       `sonar-project.properties` match the values SonarQube Cloud shows for the
       project; update them if they differ.
    4. Generate a token (**My Account → Security**) and add it to the repo as
       an Actions secret named **`SONAR_TOKEN`**
       (Settings → Secrets and variables → Actions). Do **not** set
       `SONAR_HOST_URL` — that is for self-hosted SonarQube Server only.

    Until this is done the `SonarQube Cloud` job fails for lack of a token;
    fork PRs skip it automatically (forks can't read the secret).

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

`scripts/release.ps1` does the whole dance — bump, check, commit, tag, push:

```powershell
.\scripts\release.ps1                  # patch: 0.1.0 -> 0.1.1
.\scripts\release.ps1 minor            # 0.1.0 -> 0.2.0 (also: major)
.\scripts\release.ps1 -Version 1.0.0   # set an explicit version
.\scripts\release.ps1 minor -DryRun    # preview without changing anything
```

It refuses to run unless you are on a clean, up-to-date `main`; then it bumps
the version in `pyproject.toml` + `uv.lock` (via `uv version`) and
`src/mooring/__init__.py`, runs lint and tests, commits `release: vX.Y.Z`,
tags `vX.Y.Z`, and pushes branch and tag together. It runs in Windows
PowerShell 5.1 or [pwsh](https://github.com/PowerShell/PowerShell) (so it
works on macOS/Linux too).

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
