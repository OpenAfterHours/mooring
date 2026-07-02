---
icon: lucide/sliders
---

# Configuration

Mooring reads its settings in three layers, each overriding the one before:

```
packaged defaults  ←  user config file  ←  environment variables
(config_default.toml)  (%APPDATA%\mooring\config.toml)   (MOORING_*)
```

So a baked default can be overridden per-machine by a user file, and either can
be overridden for a single run by an environment variable.

!!! tip "Most analysts never touch any of this"

    The simple path is `uvx mooring` (Python 3.12 or newer): on first run the hub
    shows a one-time [runtime setup form](#the-runtime-setup-form) where you paste the
    OAuth client id, owner, and repo, and you're done. Baking those values into a
    frozen `.pyz`/`.exe` build (the [packaged default file](#the-packaged-default-file))
    is an **optional, advanced** step for admins shipping to machines with no Python
    tooling at all.

!!! note "Running a frozen build?"

    The CLI examples below use the bare `mooring <cmd>` form (or `uvx mooring <cmd>`
    for a one-off). Running a frozen `.pyz`/`.exe` build instead? Use
    `python mooring.pyz <cmd>` (or `mooring.exe <cmd>`).

## Config keys

All keys live in `config_default.toml` (and the user `config.toml`), grouped
into three sections:

### `[github]`

| Key | Default | Meaning |
|-----|---------|---------|
| `client_id` | `""` | OAuth app **Client ID** (device flow enabled). Public, no secret. Required. |
| `owner` | `""` | GitHub org or user that owns the shared repo. Required (single-repo form). |
| `repo` | `""` | Name of the shared notebooks repo. Required (single-repo form). |
| `branch` | `"main"` | Branch to sync from / push to. |
| `host` | `"github.com"` | The GitHub instance, for [GitHub Enterprise](github-setup.md#github-enterprise) setups (e.g. `ghe.example.com`; a full URL is also accepted). One host per installation — it applies to every registered repo. |

The app is considered **configured** only when `client_id` and a repo
(owner + name) are known. Until then the hub shows the
[setup form](#the-runtime-setup-form).

### `[repos]` — multiple repos

Several repos can be registered as `[repos.<alias>]` tables; exactly one is
**active** at a time (the hub's header dropdown and `repo use` switch it):

```toml
[repos]
active = "team"            # alias of the active repo

[repos.team]
owner = "your-org"
repo = "notebooks"
branch = "main"
workspace = ""             # optional per-repo workspace override

[repos.sandbox]
owner = "your-org"
repo = "lab"
```

Rules worth knowing:

- **`active` is a reserved key**, not a valid alias.
- When **any** `[repos]` section exists, the `[github]` `owner`/`repo` keys are
  ignored (`client_id` is still read). Without one, a single repo is
  synthesized from `[github]` — that's how v0.1 configs and simple baked
  defaults keep working.
- The first time the app writes to the repo registry (hub setup card, `repo
  add`/`use`/`remove`), it converts the user file to the `[repos]` form,
  carrying over the effective `[github]` repo.

### `[sync]`

| Key | Default | Meaning |
|-----|---------|---------|
| `folders` | `["notebooks", "data", "reports"]` | Top-level repo folders to sync. Only paths under these are tracked. Applies to every registered repo. |
| `exclude` | `[]` | Extra paths to keep out of GitHub, on top of the always-skipped ones. Applies to every registered repo. See [Excluding files](#excluding-files). |
| `warn_file_mb` | `10` | Warn when pushing a file larger than this many MB. |
| `max_file_mb` | `45` | Refuse to push files larger than this. The GitHub Contents API fails somewhere below 50 MB, so don't raise it past ~45. |

!!! note "Sub-folder notebooks travel in `mooring.toml`"

    When someone creates a notebook in a sub-folder (e.g. a uv-workspace package's
    `packages/finance/notebooks/`), mooring records that folder under `[sync] folders`
    in the repo's **synced** `mooring.toml`. That list is **additive** — it *extends*
    the `folders` above for the whole team, so a teammate who pulls picks the folder
    up automatically without editing their own `config.toml`. You don't normally edit
    it by hand. (This is distinct from the per-machine `config.toml` `[sync] folders`
    here, which *replaces* the default.)

#### Excluding files

Some paths are **always** skipped in both directions, no configuration needed:

- **dotfiles** (anything starting with `.`) — **except** `.platform` files,
  which Power BI projects require;
- **`.pbi/`** folders (Power BI machine-local caches, including multi-MB
  `cache.abf` files);
- **`__pycache__/`** (CPython bytecode) and **`__marimo__/`** (marimo's
  per-session state, layout, and cache folder);
- mooring's own `.remote-<sha>` conflict scratch copies.

To skip anything else, add glob patterns to `exclude`. They are **case-sensitive**:

```toml
[sync]
exclude = [
  "*.tmp",            # any file with this name, anywhere in the tree
  "scratch",          # a file or folder named "scratch", anywhere
  "reports/drafts/*", # a pattern with "/" matches the whole path
]
```

How patterns match:

- A **bare** pattern (no `/`) matches any single path *segment*, so it catches
  both files and folders by that name at **any depth**. Useful, but note this
  means a bare pattern equal to a synced folder name — e.g. `exclude = ["data"]`
  with the default `folders` — hides that **entire** top-level folder. To target
  a nested folder only, anchor it with a `/` pattern (e.g. `"*/data/*"`).
- A pattern **containing `/`** is matched against the full repo-relative path.
  Wildcards are **not** path-aware here: `*` spans `/`, so `"reports/drafts/*"`
  also hides deeper files like `reports/drafts/sub/deep.py` (i.e. `*` behaves
  like gitignore's recursive `**`, not a single level).
- A **trailing `/`** is accepted and means the same as the bare form, so the
  familiar gitignore directory idiom `"scratch/"` works.
- A single pattern may be written as a bare string — `exclude = "*.tmp"` is the
  same as `exclude = ["*.tmp"]`.

The same `exclude` applies to the local scan **and** the remote tree, so an
excluded path stays invisible to both pull and push (it is never uploaded, and a
teammate's matching file is never pulled or deleted).

!!! note "Keeping personal drafts out of the repo"

    **Duplicate as draft** copies notebooks to `{name}-{login}-draft.py`
    siblings. Teams that never want drafts in the shared repo can exclude them:

    ```toml
    [sync]
    exclude = ["*-draft.py"]
    ```

    Mooring then refuses to *create* a new draft with a clear error instead of
    minting a file sync would never carry. The caveat: excluded files disappear
    from the hub listing **entirely** (the listing is sync-scoped), so a pattern
    added *after* drafts exist hides those local files — including any numbered
    `-draft-2.py` copies, which the bare pattern above does not match — rather
    than deleting them. Have the team clean up existing drafts before adding it.

### `[trash]`

Before mooring overwrites or removes a local file on the user's behalf (a
conflict's "Use remote", pull updates/removals, delete, a data-file revert), the
file's current bytes are saved to `<workspace>/.mooring/trash` so the action can
be undone — a toast in the hub, the Trash panel on the **Activity** page, or
`mooring trash list` / `restore`. **Strictly local**: the trash and the activity
journal live in the `.mooring` state folder, never sync to the team repo, and
are separate from [central logging](#central-logging) (which never carries file
paths or contents).

| Key | Default | Meaning |
|-----|---------|---------|
| `keep_days` | `14` | Drop saved pre-images older than this. |
| `keep_per_file` | `10` | Keep at most this many pre-images per file. |
| `max_file_mb` | `45` | Don't bank files larger than this (the action still runs). |
| `max_total_mb` | `200` | Total store cap; oldest entries evicted first. |

### `[ai]` — the copilot

The `[ai]` / `[ai.pii]` settings are documented where their privacy story lives:
[Why the copilot can't see your data](ai-privacy.md). Two knobs worth naming here
because they gate what the copilot may *read*:

| Key | Default | Meaning |
|-----|---------|---------|
| `semantic_model` | `true` | Let the copilot read a synced **Power BI semantic model** (a PBIP's TMDL): tables, columns, relationships, and measure DAX — authored code, never data. Partition/source M expressions and annotations are dropped at parse time (the table `.tmdl` is read; those parts are never captured); RLS role and translation files are never even opened. Env override: `MOORING_AI_SEMANTIC_MODEL`. Preview with `mooring ai model check`. See [the semantic model](ai-privacy.md#power-bi-semantic-model). |
| `live_schema` | `true` | Read dataframe schemas (names + types only) live from the running kernel. See [live dataframe schemas](ai-privacy.md#live-dataframe-schemas-data-outside-the-workspace). |

### `[guard]` — in the synced `mooring.toml`, not here

The **push guard** scans every outgoing file for things that look like secrets,
structured PII, or bulk data exports, and withholds flagged files behind an
explicit confirm (see [why the copilot can't see your data](ai-privacy.md) — the
same best-effort detectors watch both channels). The default policy is
`warn` (confirmable). To make findings a hard stop for the whole team, set, in
the repo's **synced** `mooring.toml` (so the policy travels with the repo and is
visible in its history):

```toml
[guard]
push = "block"   # findings must be fixed or pragma-suppressed; no override
```

A reviewed false positive is retired per line with a `# mooring: push-ok`
comment — visible in the diff, per finding. There is deliberately no global off
switch. `mooring scan` runs the same scan without pushing, and `mooring recall`
/ the hub's **Recall push** undoes the last push on the branch head (the pushed
commit remains in git history — a leaked secret must still be rotated).

### `[workspace]`

| Key | Default | Meaning |
|-----|---------|---------|
| `path` | `""` | Override the workspace location (single-repo form; with `[repos]`, use the per-repo `workspace` key). Empty means `~/PythonProjects/mooring/<owner>/<repo>`. Supports `~` expansion. |

### `[logging]`

| Key | Default | Meaning |
|-----|---------|---------|
| `endpoint` | `""` | Where to send usage/error events. Empty disables logging. See [Central logging](#central-logging) for the auto-detected URL-vs-path behaviour. |
| `level` | `"info"` | `"info"` logs usage events **and** errors; `"error"` logs only errors. |

## The runtime setup form

This is the path **most analysts** take. With a plain `uvx mooring` (no baked
config), the hub shows a setup card instead of the file list on first run. The
analyst enters:

- **OAuth client id** (only asked on first setup)
- **GitHub URL** (only for [GitHub Enterprise](github-setup.md#github-enterprise);
  leave empty for github.com — only asked on first setup)
- **Repo owner**
- **Repo name**
- **Branch** (defaults to `main`)
- **Short name** (optional alias; defaults to the repo name)

On save, the hub registers the repo in the user `config.toml` (shown below) and
reloads — no rebuild needed. The same card (via **+ Add repo…** in the header
dropdown) registers additional repos later; each save adds to the registry
rather than replacing it.

## The packaged default file

!!! info "Advanced: only for frozen builds"

    You only need this if you're baking a frozen `.pyz`/`.exe` for machines with no
    Python tooling. Teams on the `uvx mooring` path skip it and use the
    [runtime setup form](#the-runtime-setup-form) above instead.

`src/mooring/config_default.toml` is baked into every build. Edit it **before
building** so your team receives a pre-configured app:

```toml
[github]
client_id = "Ov23li..."   # from your OAuth app
owner = "your-org"         # owner of the notebooks repo
repo = "notebooks"         # name of the notebooks repo
branch = "main"

[sync]
folders = ["notebooks", "data", "reports"]
exclude = []               # extra paths to skip; see Excluding files above
warn_file_mb = 10
max_file_mb = 45

[workspace]
path = ""                  # empty = ~/PythonProjects/mooring/<owner>/<repo>

[logging]
endpoint = ""              # optional; see Central logging below
level = "info"
```

To bake **several** repos in, use the `[repos]` form shown above instead of
the `[github]` `owner`/`repo` keys (keep `client_id` in `[github]`).

Where these values come from is covered in [GitHub setup](github-setup.md).
Building is covered in [Build & distribute](build-and-distribute.md).

## The user config file

Each machine can override the baked defaults with a `config.toml` in the
per-user config directory (same keys and sections as above):

=== "Windows"

    ```
    %APPDATA%\mooring\config.toml
    ```

=== "macOS"

    ```
    ~/Library/Application Support/mooring/config.toml
    ```

=== "Linux"

    ```
    ~/.config/mooring/config.toml
    ```

The stored GitHub **token** lives in the same directory (named `token`) only as
a fallback when no OS credential store is available; normally it's in the
credential store instead. Run `selftest` to print the exact paths on a given
machine.

## Editing the user config from the command line

Rather than hand-editing the file, `mooring config` reads and writes it by
**dotted key** (`section.subsection.key`). It touches only the user file and
leaves every other setting in place:

```
mooring config set ai.pii.enabled true          # bool
mooring config set ai.pii.name_threshold 0.6     # number
mooring config set ai.pii.name_labels person name organization   # several tokens = a list
mooring config get ai.pii.enabled                # print the effective value
mooring config unset ai.pii.enabled              # remove the key (revert to the default)
mooring config list                              # print the whole effective config
mooring config path                              # print the config.toml location
```

Value typing is automatic: `true`/`false` become booleans, `5`/`0.6` become
numbers, **several tokens become a string list**, and anything else (a path or
model id like `urchade/gliner_multi_pii-v1`) stays a string. To force a string
that looks numeric, quote it as a TOML literal, e.g. `set some.key '"123"'`.

`get` and `list` show the **effective** value (packaged default merged with your
file); they do **not** reflect a one-run [environment-variable](#environment-variables)
override. This works for any key, including the `[ai]` / `[ai.pii]` settings
documented in [AI privacy](ai-privacy.md).

!!! note "Per-notebook and per-model AI opt-outs live elsewhere"
    Turning the copilot off for a single notebook — or for a single Power BI
    semantic model (`[ai] disabled_semantic_models`) — is **not** a
    `mooring config` setting: both are written to a synced `mooring.toml` at the
    workspace root so the decision travels with the repo. See
    [Turning the copilot off for a notebook](ai-privacy.md#turning-the-copilot-off-for-a-notebook)
    and [the semantic model](ai-privacy.md#power-bi-semantic-model).

## Environment variables

Any of these override both config files for a single run. They're mainly for
integration testing and CI, but work anywhere:

| Variable | Overrides |
|----------|-----------|
| `MOORING_CLIENT_ID` | `[github] client_id` |
| `MOORING_GITHUB_HOST` | `[github] host` — the GitHub instance to talk to. |
| `MOORING_ACTIVE_REPO` | `[repos] active` — selects which registered repo is active. |
| `MOORING_OWNER` | The active repo's `owner` |
| `MOORING_REPO` | The active repo's `repo` |
| `MOORING_BRANCH` | The active repo's `branch` |
| `MOORING_WORKSPACE` | The active repo's workspace path |
| `MOORING_TOKEN` | The stored auth token — set this to skip device-flow login entirely (a personal access token works). |
| `MOORING_TRUSTSTORE` | Set to `0` to disable [OS trust store TLS verification](#corporate-networks-tls) and fall back to the bundled CA list. |
| `MOORING_LOG_ENDPOINT` | `[logging] endpoint` — the central log destination (see [Central logging](#central-logging)). |
| `MOORING_LOG_LEVEL` | `[logging] level` — `info` or `error`. |

See [Contributing](../developers/contributing.md#integration-testing) for using
these to test against a scratch repo.

## Advanced / IT governance

The rest of this page is for administrators rolling mooring out across a team —
fleet-wide telemetry and corporate-network TLS. Individual analysts on the
`uvx mooring` path don't need any of it.

### Central logging

Set `[logging] endpoint` (baked into the build, or in a user `config.toml`) to
collect a record of how the app is used and what fails, from every copy, in one
place. It is **off by default** — no endpoint, no logging. When an endpoint is
set, logging is always on for users (there is no per-user off switch).

The value is **auto-detected**:

- An `http://` / `https://` URL → each event is **POSTed as JSON** to that URL.
  HTTPS uses the OS trust store like the rest of the app, so a corporate
  proxy's root CA is honoured automatically.
- Anything else is treated as a **folder or UNC path** → events are appended to
  a per-user file `<os-user>@<host>.jsonl` in that folder (e.g.
  `\\fileserver\share\mooring-logs`). One file per user means no write
  contention between teammates on a shared drive.

```toml
[logging]
endpoint = "https://collector.example.com/mooring"   # or \\server\share\mooring-logs
level = "info"   # "info" = usage + errors; "error" = errors only
```

#### What gets logged

Each event is one JSON object: a UTC timestamp, the event name, identity, and a
few event-specific fields. Identity is **OS username, hostname, app version, OS,
Python version, and the GitHub login** (added once the user has logged in).

```json
{"ts":"2026-06-13T12:34:56.789Z","event":"push","version":"0.2.2",
 "os_user":"jdoe","host":"FIN-LT-042","os":"Windows-11-10.0.26200",
 "python":"3.13.7","user":"octocat","pushed":3,"conflicts":0,"lines":4}
```

Events cover the app/command start, login/logout, repo add/switch/remove,
pull/push/propose (with counts), open/new, and errors (`event:"error"` with the
exception type and message). **No file contents, file paths, or full tracebacks
are ever sent** — only counts and coarse kinds. An error message may incidentally
contain a URL or path.

Logging is strictly best-effort: it runs on a background thread, never blocks a
command, and silently drops events if the destination is slow or unreachable
(the process still exits within a few seconds). `mooring selftest` prints a
`logging` line showing the active destination.

### Corporate networks & TLS

Mooring verifies TLS connections against the **operating system's trust
store** (via [truststore](https://truststore.readthedocs.io/), the same
mechanism pip uses). In practice:

- On corporate networks with **SSL-intercepting proxies**, IT installs the
  proxy's root CA into Windows' certificate store — mooring picks it up
  automatically, no configuration needed. Without this, GitHub connections
  fail with certificate-verification errors during the TLS handshake.
- On normal networks nothing changes: the OS store also trusts the public
  CAs that github.com uses.
- `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` still take precedence when set, for
  environments that pin an explicit CA bundle.
- `MOORING_TRUSTSTORE=0` turns the behavior off entirely (escape hatch).

`mooring selftest` prints a `tls trust` line showing which mode is active.

!!! note "Changing `host` means logging in again"

    Tokens are stored **per GitHub host**, so a token obtained from one
    instance is never sent to another. After pointing an existing
    installation at a different `host`, run `mooring login` (or use the hub's
    login button) once.
