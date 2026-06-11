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
| `owner` | `""` | GitHub org or user that owns the shared repo. Required. |
| `repo` | `""` | Name of the shared notebooks repo. Required. |
| `branch` | `"main"` | Branch to sync from / push to. |

The app is considered **configured** only when `client_id`, `owner`, and `repo`
are all non-empty. Until then the hub shows the [setup form](#the-runtime-setup-form).

### `[sync]`

| Key | Default | Meaning |
|-----|---------|---------|
| `folders` | `["notebooks", "data"]` | Top-level repo folders to sync. Only paths under these are tracked. |
| `warn_file_mb` | `10` | Warn when pushing a file larger than this many MB. |
| `max_file_mb` | `45` | Refuse to push files larger than this. The GitHub Contents API fails somewhere below 50 MB, so don't raise it past ~45. |

### `[workspace]`

| Key | Default | Meaning |
|-----|---------|---------|
| `path` | `""` | Override the workspace location. Empty means `~/Documents/mooring/<repo>`. Supports `~` expansion. |

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
folders = ["notebooks", "data"]
warn_file_mb = 10
max_file_mb = 45

[workspace]
path = ""                  # empty = ~/Documents/mooring/<repo>
```

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
one-time setup card instead of the file list. The analyst enters:

- **OAuth client id**
- **Repo owner**
- **Repo name**
- **Branch** (defaults to `main`)

On save, the hub writes those four values to the user `config.toml` shown above
and reloads — no rebuild needed. This is handy for pilots or when you can't
re-distribute a build. `client_id`, `owner`, and `repo` are all required;
`branch` defaults to `main` if left blank.

## Environment variables

Any of these override both config files for a single run. They're mainly for
integration testing and CI, but work anywhere:

| Variable | Overrides |
|----------|-----------|
| `MOORING_CLIENT_ID` | `[github] client_id` |
| `MOORING_OWNER` | `[github] owner` |
| `MOORING_REPO` | `[github] repo` |
| `MOORING_BRANCH` | `[github] branch` |
| `MOORING_WORKSPACE` | `[workspace] path` |
| `MOORING_TOKEN` | The stored auth token — set this to skip device-flow login entirely (a personal access token works). |

See [Contributing](../developers/contributing.md#integration-testing) for using
these to test against a scratch repo.
