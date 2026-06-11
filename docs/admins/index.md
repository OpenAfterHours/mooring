---
icon: lucide/settings
---

# Admin overview

As an admin you set up the shared GitHub repo, register the OAuth app, bake the
configuration into the build, and distribute the app to your analysts. This
section walks through each step.

## What your analysts experience

Once you've done the setup below, an analyst's entire experience is:

1. Install Python 3.12.
2. Run the app file you gave them.
3. Log in with a GitHub device code.
4. Pull / edit / push.

No git, no pip, no config — because you baked it in.

## End-to-end checklist

- [ ] **Create the shared repo** with empty `notebooks/` and `data/` folders —
      [GitHub setup](github-setup.md#create-the-shared-repo)
- [ ] **Register a GitHub OAuth app** and **enable Device Flow**; copy the
      client id — [GitHub setup](github-setup.md#register-the-oauth-app)
- [ ] **Approve the OAuth app** for your org if it restricts third-party apps —
      [GitHub setup](github-setup.md#organization-approval)
- [ ] **Bake the config** (`client_id`, `owner`, `repo`, `branch`) into
      `config_default.toml` — [Configuration](configuration.md)
- [ ] **Build** the `.pyz` / `.exe` (or a no-Python bundle) —
      [Build & distribute](build-and-distribute.md)
- [ ] **Distribute** the artifact to your team —
      [Build & distribute](build-and-distribute.md#distribute)

!!! tip "The four values you need from GitHub"

    `client_id`, `owner`, `repo`, and `branch`. Where each one comes from — and
    every click to get them — is in [GitHub setup](github-setup.md).

## The two ways to configure a team

| Approach | How analysts get config | Best for |
|----------|-------------------------|----------|
| **Baked** (recommended) | You edit `config_default.toml` and build; analysts get a ready-to-use app | Most teams |
| **Runtime setup form** | You distribute an unconfigured build; each analyst types `client_id` / `owner` / `repo` / `branch` into a one-time form in the hub | Pilots, mixed repos, or when you can't rebuild |

Both are covered in [Configuration](configuration.md).
