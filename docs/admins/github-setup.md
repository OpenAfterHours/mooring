---
icon: lucide/key-round
---

# GitHub setup

This is the page to obtain **every GitHub detail mooring needs**. By the end you
will have four values to configure the app — and you'll know exactly where each
one came from.

!!! note "Running a frozen build?"

    CLI examples here use the bare `mooring <cmd>` form (install with `uvx
    mooring`). Running a frozen `.pyz`/`.exe` build instead? Use `python
    mooring.pyz <cmd>` (or `mooring.exe <cmd>`) in their place.

## What you'll end up with

Once these four values exist, analysts get the whole mooring workflow with **no
git and no personal access tokens to juggle** — just Python 3.12 or newer and a
device-flow sign-in. The values below are the only GitHub plumbing the team ever
touches.

| Value | Config key | Comes from |
|-------|------------|------------|
| OAuth app **Client ID** (e.g. `Ov23li…`) | `client_id` | [Registering the OAuth app](#register-the-oauth-app) |
| Repo **owner** (org or username) | `owner` | [Creating the shared repo](#create-the-shared-repo) |
| Repo **name** | `repo` | [Creating the shared repo](#create-the-shared-repo) |
| Branch to sync (usually `main`) | `branch` | The repo's default branch |
| GitHub **host** (only for GitHub Enterprise) | `host` | [GitHub Enterprise](#github-enterprise) |

Once you have all four (five on GitHub Enterprise), plug them in via
[Configuration](configuration.md).

!!! note "No client secret"

    Mooring uses GitHub's **OAuth Device Flow**, which authenticates with only
    a *public* client id. There is **no client secret** to copy, store, or
    rotate.

## Create the shared repo

One repository holds the whole team's notebooks. Everyone pulls from and pushes
to it.

1. On GitHub, create a new repository — for example `your-org/notebooks`. It can
   be **private**; analysts authenticate as themselves.
2. Add two top-level folders the app syncs by default: **`notebooks/`** and
   **`data/`**. GitHub won't let you commit an empty folder, so add a
   placeholder file in each (e.g. a `.gitkeep`).
3. Note the **owner** (the org or username before the `/`) and the **repo name**
   — these become `owner` and `repo`.
4. The **branch** mooring syncs is `main` by default; set `branch` if your
   default branch is named differently.

!!! tip "Want changes reviewed before they land?"

    Analysts can use **Propose** instead of Push to upload changes to a
    personal `mooring/<username>/...` review branch and open a pull request
    (see [Proposing changes](../users/daily-workflow.md#proposing-changes-for-review)).
    To **require** review, protect the shared branch with a GitHub branch
    protection rule — direct pushes are then rejected and Propose becomes the
    only way in. Analysts still need *write* permission on the repo so they
    can create the review branches.

!!! warning "Don't enable Git LFS on this repo"

    Mooring reads file contents through the GitHub API. For LFS-tracked files
    the API serves the small **pointer file**, not the real content, so
    notebooks and data would sync as broken stubs. Keep this repo LFS-free and
    [keep large files out of it](../users/daily-workflow.md#where-your-files-live).

## Register the OAuth app

This produces the **Client ID** — the one value that isn't in the repo itself.

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.
    - Register it under the **organization** that owns the repo if you want the
      org to manage it; otherwise a personal app is fine.
2. Fill in the form:
    - **Application name** — anything, e.g. `Mooring`.
    - **Homepage URL** — any URL (it isn't used by device flow), e.g. your
      team wiki or `https://example.com`.
    - **Authorization callback URL** — any URL; device flow doesn't use a
      callback, so this is just a required placeholder.
3. Click **Register application**.
4. On the app's page, **enable Device Flow**:

    !!! warning "This is the step everyone forgets"

        Find **“Enable Device Flow”** and turn it on, then save. Without it,
        login fails with an error from GitHub because the app isn't allowed to
        request device codes.

5. Copy the **Client ID** (a string like `Ov23li…`). This becomes `client_id`.
   You do **not** need to generate or copy a client secret.

## Token scopes & what they grant

When an analyst logs in, mooring requests the **`repo`** scope. That's enough to
read the tree and blobs and to commit via the Contents API.

!!! warning "`repo` is broad"

    The `repo` scope grants read/write access to **every repository the user can
    already reach**, not just your notebooks repo — that's how GitHub OAuth
    scopes work. The token is stored locally in the user's OS credential store
    (see below), never sent anywhere but GitHub. If that breadth is a concern,
    have analysts use accounts in a dedicated machine-account org whose only
    access is the notebooks repo.

!!! note "The AI copilot signs in separately"

    This `repo` token is only for syncing notebooks. The optional AI copilot
    authenticates through its **own** GitHub Copilot sign-in and is
    schema-only — it sees your column names and types and your notebook's code,
    but never the data itself. See
    [Why it cannot see your data](ai-privacy.md).

## Organization approval

If your notebooks repo lives in an **organization that restricts third-party
OAuth apps**, the app must be approved before anyone can log in:

- An **org owner** approves it under **Org → Settings → Third-party access →
  OAuth app access policy** (or via the request an analyst triggers on first
  login).
- Until it's approved, analysts see a GitHub message asking them to request
  access. Approving once covers the whole team.

??? info "How login actually works (device flow)"

    1. The app POSTs to `https://{your-github-host}/login/device/code` with
       your `client_id` and the `repo` scope, and gets back a short **user
       code** and the verification URL `https://{your-github-host}/login/device`.
    2. The analyst opens that URL, enters the code, and authorizes the app.
    3. Meanwhile the app polls
       `https://{your-github-host}/login/oauth/access_token` until GitHub
       returns an **access token**.
    4. The token is saved to the OS credential store — **Windows Credential
       Manager** / **macOS Keychain** via `keyring` — with a permission-locked
       plaintext file (`token` next to `config.toml`) as a fallback when no
       credential store is available. `logout` deletes it.

    For CI or scripted testing you can bypass device flow entirely by setting
    the `MOORING_TOKEN` environment variable (a personal access token works) —
    see [Configuration](configuration.md#environment-variables).

## GitHub Enterprise

If your GitHub is a **GitHub Enterprise** instance (say
`https://ghe.example.com/` instead of `github.com`), everything above still
applies — it just happens on *your* instance:

1. Set the **`host`** config key (in `[github]`, next to `client_id`) to your
   instance — a bare host like `ghe.example.com` or a full URL like
   `https://ghe.example.com/` both work. There is one host per installation;
   all registered repos live on it. From the CLI:
   `mooring repo add your-org/notebooks --host ghe.example.com`.
2. Register the OAuth app **on your instance** (same path: your GHE →
   Settings → Developer settings → OAuth Apps), not on public github.com — a
   github.com client id will not work against an Enterprise host.

    !!! warning "Enable Device Flow there too"

        The ["step everyone forgets"](#register-the-oauth-app) applies on
        Enterprise just the same: enable **Device Flow** on the OAuth app you
        registered on your instance.

3. mooring derives the API endpoint automatically: `https://{host}/api/v3`
   for GitHub Enterprise Server, `https://api.{host}` for GitHub Enterprise
   Cloud data-residency hosts (`*.ghe.com`).

!!! note "GitHub Enterprise Server version"

    mooring sends the `X-GitHub-Api-Version: 2022-11-28` header, which GitHub
    Enterprise Server supports from **3.9** onward. On older GHES versions API
    calls fail with a version error — upgrade the instance or ask your GitHub
    admins which version you're on.

If analysts log in behind a corporate SSL-intercepting proxy, see
[Corporate networks & TLS](configuration.md#corporate-networks-tls) — mooring
verifies TLS against the OS trust store, so the usual corporate root CA setup
just works.

## Plug the values in

You now have `client_id`, `owner`, `repo`, and `branch`. Configure them either
by:

- **The runtime setup form (simplest)** — share the four values with your team
  and have each analyst paste them once into the hub. Nothing to build: they
  install with `uvx mooring`, open the hub, and fill in the form. See
  [Configuration](configuration.md#the-runtime-setup-form).
- **Baking them into the build (advanced)** — for the frozen `.pyz`/`.exe`
  fallback, edit `config_default.toml` before building so analysts receive a
  ready-to-use app. See [Configuration](configuration.md) and
  [Advanced: offline / frozen builds](build-and-distribute.md).
