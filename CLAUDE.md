# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Mooring is **git-free [marimo](https://marimo.io) notebook sharing via the GitHub REST API**. It ships as a single-file app (`mooring.pyz` / `mooring.exe`) or from PyPI (`uvx mooring`). Analysts pull/edit/push notebooks in a shared GitHub repo with **no git and no PAT-juggling on their machine** — only Python 3.12+. The app opens a local browser **hub** (Starlette on `127.0.0.1`) and edits notebooks in a bundled **marimo** subprocess.

## Commands

Everything runs through [uv](https://docs.astral.sh/uv/). There is no Makefile.

```bash
uv sync                                # install all deps incl. dev group
uv sync --extra copilot                # also pull github-copilot-sdk (needed for AI chat-session tests)
uv run pytest                          # full Python suite — offline, GitHub is mocked with `responses`
uv run pytest tests/test_sync.py       # one file
uv run pytest tests/test_sync.py::test_name -q   # one test
uv run ruff check src tests            # lint (line length 100)
uv run lint-imports                    # enforce the architecture layering in .importlinter
node --test tests/js/                  # JS tests for the chat REPL's pure helpers (no deps)
uv run mooring hub                     # run the app from source
uv run mooring <status|pull|push|...>  # run any CLI subcommand from source
```

CI (`.github/workflows/ci.yml`, **Windows** to match the release target) runs, in order: `ruff` → `lint-imports` → `pytest` → the Node JS tests. Run the same locally before pushing. Windows is the primary platform — avoid POSIX-only assumptions.

Integration testing against a live repo: set `MOORING_TOKEN` (a PAT skips device flow), `MOORING_CLIENT_ID`, `MOORING_OWNER`, `MOORING_REPO`, then `uv run mooring pull` etc. These env vars override both config files. Use a throwaway repo — pushes create real commits.

## Architecture

### Layered modules (enforced — read `.importlinter` before moving code)

The flat `src/mooring/` namespace hides a strict dependency direction that `uv run lint-imports` enforces. Imports may only point **down**:

```
L4  cli.py, hub/            two sibling presentation adapters (hub must not import cli)
L3  ai/*                    AI orchestration + privacy/safety
L2  sync, manifest, pbip, deletion   domain core  ·  editor, schema, marimo_rt   marimo bridge
L1  config, config_store, auth, github, runtime, ai_config   identity + config
L0  githost, paths, gitsha  stdlib-pure leaves (import nothing else in mooring)
```

Two consequences worth internalizing: the **sync domain core never imports `ai/` or `editor`**, and **`ai/` reaches marimo and raw HTTP only through `marimo_rt.py`** (the transport seam) — a direct `ai → marimo`/`urllib`/`http` import is a contract violation. Adding a backwards import will fail CI even if tests pass.

### Sync engine — the product core

No git, ever. `sync.py` does **three-way change detection** by comparing three SHAs per file: the local blob SHA (`gitsha.py`, computed the way git would), the last-synced SHA (`manifest.py`), and the remote SHA. `github.py` is a thin REST client — **reads** via the Git Data API (refs → commits → trees → blobs, downloading only changed blobs); **writes** via the Contents API, whose `sha` parameter gives per-file optimistic concurrency so GitHub itself rejects a stale write instead of clobbering a teammate. **Conflicts are never silent**: pull skips conflicted files, push blocks them for per-file resolution. `propose` writes to a personal review branch (for PR-gated repos) instead of committing straight to the shared branch.

### marimo editor subprocess

`editor.py` launches/tears down the marimo editor. Two delivery modes share one source of truth: with **uv** present, notebooks run against the repo's own `pyproject.toml` + `uv.lock` (synced via GitHub, managed by `pyproject_env.py` and the `mooring deps` commands); for **frozen** `.pyz`/`.exe`, an admin pre-builds a bundle from those same deps (no pip at runtime). Mooring's own runtime ships lean — a repo's notebook packages live with the repo, not in mooring.

### AI copilot — structurally value-blind (`ai/`)

The copilot (opt-in `mooring[copilot]` extra) is designed so the model **structurally cannot see data** — only schema (column names + types) and notebook source. Before touching anything in `ai/`, read `docs/admins/ai-privacy.md`; the value-blindness is a maintained security property with dedicated tests, not an accident. Key invariants:

- System context is assembled in **one** place (`ai/chat.py:build_system_context`).
- The agent gets only mooring's own value-free tools (`ai/tools.py`); the SDK's file/shell tools are removed, a deny-all permission handler backstops, and it runs in an empty working directory.
- Applying a cell writes **source only** via marimo codegen (`ai/cellwrite.py`); mooring never opens a marimo websocket (the only channel carrying outputs/values). Live-schema introspection (`ai/introspect.py`) pushes a fixed, value-free probe over HTTP and reads back only names+dtypes.
- The egress guard (`ai/egress.py`, `pii.py`, `secrets.py`, `ner.py`/`ner_spacy.py`) scans text leaving the workspace for structured PII / names; findings are value-free `(line, kind)` pairs. PII name detection is behind the `pii` (GLiNER) or `pii-spacy` (offline) extras.

Tests that pin these guarantees use a `SECRET_VALUE_DO_NOT_LEAK` fixture and assert it never reaches the model (`test_schema.py`, `test_ai_tools.py`, `test_chat_session.py`, `test_introspect.py`, …). Keep them green.

### Config layering

`config.py` merges, lowest-to-highest precedence: baked `config_default.toml` → user `config.toml` (`mooring config` commands edit it) → `MOORING_*` env vars. Separately, `workspace_config.py` reads a **synced** `mooring.toml` at the workspace root for per-repo, travels-with-the-repo settings (e.g. `[ai] disabled_notebooks`).

## Gotchas

- **PYTHONPATH for the marimo subprocess.** When packaged, moonlit activates its extracted site-packages via `site.addsitedir()`, which child processes don't inherit. `cli._ensure_child_pythonpath()` re-exposes them on `PYTHONPATH` so marimo and its kernels can import the bundled stack. **Don't remove it.**
- **UTF-8 BOM breaks notebooks.** marimo rejects `.py` notebooks starting with a BOM. Use a BOM-less writer when generating notebook files (PowerShell 5.1's `Out-File -Encoding utf8` writes one).
- **`httpx` vs `httpx2`.** Starlette's test client prefers `httpx2`; the project pins plain `httpx` (behind a deprecation warning) as the conservative choice.
- The `mooring-spacy-en-md` model companion under `packages/` is wired as a **path dependency, not a uv workspace member** — a workspace breaks `uv build`/moonlit. Don't convert it.

## Releasing

`scripts/release.ps1` (PowerShell 5.1 or pwsh) does the whole dance: refuses unless on a clean up-to-date `main`, bumps the version in `pyproject.toml` + `uv.lock` (`uv version`) and `src/mooring/__init__.py`, runs lint+tests, commits `release: vX.Y.Z`, tags, and pushes. The tag triggers `release.yml`, which re-checks, builds the frozen artifacts, attaches them to a GitHub Release, and publishes to PyPI — with a guard that fails if the tag and the two version strings disagree. Keep those three versions in sync.

## Docs

Documentation lives in `docs/` and builds with [zensical](https://zensical.org) (`uv run zensical serve|build`), publishing to GitHub Pages via `docs.yml`. `docs/developers/` covers architecture and contributing; `docs/admins/ai-privacy.md` is the canonical spec for the copilot's value-blindness.
