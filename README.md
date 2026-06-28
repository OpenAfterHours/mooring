# <img src="docs/assets/images/anchor-mark.svg" alt="" height="28"> mooring

**Analyse your data with an AI copilot that never sees your data** — then share
that work across your team over GitHub, with no git and no tokens. For teams of
data analysts on nothing but **Python 3.12+** (`uvx mooring`).

**An AI copilot that can't see your data.** Mooring ships an optional,
interactive copilot — backed by GitHub Copilot — that helps analysts write
notebook code while being **structurally unable to see the data itself**. The
model only ever receives a dataset's **schema** (column names + dtypes), the
notebook's **source code**, and your chat turns — never a value, a cell output,
or the contents of a data file. It *proposes* cells: you review the diff and
Apply, and any Apply can be rolled back. The copilot is off until you install
the `copilot` extra, sign in to Copilot, and have your organisation's Copilot
agent policy enabled — so the git-free workflow below stands entirely on its
own.

**Share without git, and pick up where a teammate left off.** Pull, edit, and
push marimo notebooks stored in a shared GitHub repo — **without git installed
on their machines** and with **no personal access tokens to juggle**. All sync
happens over the GitHub REST API; the only requirement on an analyst's machine
is Python 3.12 or newer. Running the app opens a local browser **hub**: log in
with a one-time device code, see every team notebook with its sync status, pull
the latest, open a teammate's notebook in the bundled marimo editor (in the same
locked environment they used), and push your changes back — one commit per file,
with conflicts detected and resolved per file (never silently overwritten).

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

## A schema-only AI copilot

Mooring's second pillar is an interactive AI copilot for the marimo editor —
opt-in, and designed so the model **cannot see your data**. It's backed by GitHub
Copilot and runs *beside* a notebook in a second browser tab, proposing cells you
review and Apply. The dataset/notebook guarantee is *structural*; the extra
scanners below are honest, opt-in safety nets layered on top. The full write-up
and verify-it-yourself tests are in
[why the copilot can't see your data](docs/admins/ai-privacy.md).

- **Schema and source only — never values.** A single context assembler is the
  one place the model's context is built: a dataset's **schema** (column names +
  dtypes, read from a parquet footer or a csv/xlsx header — no row is ever
  materialised), the notebook's `.py` **source**, and your chat turns. No cell
  outputs, no variable values, no data-file contents. This is enforced by tests
  and an import-linter contract, so a new egress path that skips scrubbing is a
  review-visible change to that one file.
- **The agent has no tool that can read data.** The Copilot agent runs with tool
  use restricted to mooring's own value-free toolset (an explicit allowlist), a
  deny-all permission backstop, and an empty working directory — so it has no
  path to read a data file or a cell output (verified against
  `github-copilot-sdk` v1.0.1). marimo's own AI assistant (which *does* send
  sample values) is disabled in every editor mooring launches.
- **It proposes; you Apply; you can roll back.** The copilot never writes the
  notebook itself. Each edit is a structured proposal — a diff you review — that
  Apply lands through marimo's own codegen, atomically, with a conflict error if
  the target cell changed underneath you. The hub snapshots the pre-edit bytes,
  so any Apply can be rolled back.
- **Live-kernel schemas, still value-free.** Real data often lives *outside* the
  workspace, or in a derived frame no file holds. Mooring can read the schema of
  dataframes already loaded in your running kernel via a **fixed, mooring-authored
  probe** over marimo's run endpoint — it never opens the kernel websocket, never
  reads an output, emits only names + dtypes, and parses the result fail-closed.
  On by default for the copilot; falls back to the file schema on any hiccup.
- **An off switch that travels.** Turn the copilot off for one notebook (e.g.
  once it handles real values) from the hub or the chat bar; the opt-out is
  re-checked on every open, send, and apply, and rides pull/push in a synced
  `mooring.toml` that stores only paths — never a value.

### Defence in depth (opt-in, off by default)

The schema-only contract above stops the *data* from reaching the model. These
layers add a best-effort floor against a human **typing a value** into a cell or
a chat message — **off by default**, precision over recall, and explicitly *not*
a proof of value-freedom.

- **Structured-PII guard** (`[ai.pii] enabled`, default off) — an offline,
  pure-stdlib scan for checksum-validated payment cards, IBANs, and NHS numbers
  plus shape-anchored emails and UK NINOs, at every egress. It can
  **warn-and-confirm** ("Send anyway") before a prompt leaves — it does not
  *block*, and it fails *open* on a scan error. Findings are value-free (a line
  and a *kind*, never the matched value). It cannot catch names, addresses, sort
  codes, account numbers, SSNs, phone numbers, DOBs, or IPs — a clean scan is not
  a value-free guarantee.
- **Local name detection** (opt-in, on top of the PII guard) — a local zero-shot
  NER pass (GLiNER via the `pii` extra, or fully offline spaCy via `pii-spacy`
  for air-gapped teams) that flags person/org names **on the analyst's own
  machine**; no text is sent anywhere to be scanned. GLiNER downloads a pinned
  safetensors model from Hugging Face on first use.
- **Team context** (`[ai] context`, default off) — an opt-in
  `context/instructions.md` plus a dbt-first data dictionary, so the copilot
  knows your real tables and joins. Dictionary fields are restricted to a fixed
  **five-slot allowlist** (column name/type/nullable/relationship/description,
  table name/description) — a parser cannot add a slot; `instructions.md` is
  size-capped, secret- and PII-scanned, and withheld entirely on a high-confidence
  hit. Unlike the file schema, this is *free text a human wrote*: minimised,
  scanned, and human-reviewed — **not** structurally value-free. Treat it like
  code: review it, never paste real values.

**Requirements.** The copilot needs the `copilot` extra installed
(`pip install "mooring[copilot]"`), an in-app GitHub Copilot sign-in (separate
from your GitHub login — it can be a different account, via the SDK's device
flow), a Copilot licence, **and** your organisation's Copilot CLI/agent policy
enabled. Without the policy the request is rejected. See the
[copilot guide](docs/users/ai-copilot.md) and the
[privacy page](docs/admins/ai-privacy.md).

## Install from PyPI

With **Python 3.12+** and [uv](https://docs.astral.sh/uv/), run Mooring straight
from PyPI — no frozen build needed:

```
uvx mooring                  # run it as a one-off tool
uv tool install mooring      # …or install it as a persistent CLI
pip install mooring          # …or into the active environment
```

### Optional extras

Mooring ships lean; opt-in features live behind extras. **Quote the brackets** —
`[...]` is a shell glob, so an unquoted `mooring[copilot]` can expand to nothing:

| Extra | Enables |
|-------|---------|
| `copilot` | the AI copilot |
| `pii` | NER name detection for the PII guard |
| `pii-spacy` | offline name detection (air-gapped teams) |

```
uvx "mooring[copilot]"               # one-off tool run
uv tool install "mooring[copilot]"   # persistent CLI tool
uv add "mooring[copilot]"            # add to your own uv project
pip install "mooring[copilot]"       # plain pip
```

Combine with a comma (`"mooring[copilot,pii]"`). Full reference:
[optional extras](docs/admins/build-and-distribute.md#optional-extras).

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

## License

Mooring is released under the [MIT License](LICENSE).

---

Built with [moonlit](https://github.com/openafterhours/moonlit).
