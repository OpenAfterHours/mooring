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
| `warn_file_mb` | `10` | Warn when pushing a file larger than this many MB. |
| `max_file_mb` | `45` | Refuse to push files larger than this. The GitHub Contents API fails somewhere below 50 MB, so don't raise it past ~45. |

Within synced folders, dotfiles are skipped — **except** `.platform` files,
which Power BI projects require. `.pbi/` folders (Power BI machine-local
caches, including multi-MB `cache.abf` files) are never synced in either
direction.

### `[workspace]`

| Key | Default | Meaning |
|-----|---------|---------|
| `path` | `""` | Override the workspace location (single-repo form; with `[repos]`, use the per-repo `workspace` key). Empty means `~/Documents/mooring/<owner>/<repo>`. Supports `~` expansion. |

## The packaged default file

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
warn_file_mb = 10
max_file_mb = 45

[workspace]
path = ""                  # empty = ~/Documents/mooring/<owner>/<repo>
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

## The runtime setup form

If a build ships **without** `client_id` / `owner` / `repo`, the hub shows a
setup card instead of the file list. The analyst enters:

- **OAuth client id** (only asked on first setup)
- **GitHub URL** (only for [GitHub Enterprise](github-setup.md#github-enterprise);
  leave empty for github.com — only asked on first setup)
- **Repo owner**
- **Repo name**
- **Branch** (defaults to `main`)
- **Short name** (optional alias; defaults to the repo name)

On save, the hub registers the repo in the user `config.toml` shown above and
reloads — no rebuild needed. The same card (via **+ Add repo…** in the header
dropdown) registers additional repos later; each save adds to the registry
rather than replacing it.

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

See [Contributing](../developers/contributing.md#integration-testing) for using
these to test against a scratch repo.

## Corporate networks & TLS

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
