---
icon: lucide/settings
---

# Admin overview

As an admin you set up the shared GitHub repo, register the OAuth app, and choose
how analysts get their config. Your real job is enabling the analyst experience:
schema-only AI analysis, GitHub with no git and no tokens to juggle, and easy
sharing across the team. This section walks through each step.

## What your analysts experience

Once you've done the setup below, an analyst's entire experience is:

1. Install Python 3.12+ and run `uvx mooring`.
2. Log in with a GitHub device code.
3. Pull / edit / push.

No git, no PAT-juggling, no config — just Python 3.12 or newer.

If you enable it, analysts also get an opt-in **AI copilot** in the editor that
is schema-only — it sees your column names and types and your notebook's code, but
never the data itself. See [Why it cannot see your data](ai-privacy.md).

## End-to-end checklist

- [ ] **Create the shared repo** with empty `notebooks/` and `data/` folders —
      [GitHub setup](github-setup.md#create-the-shared-repo)
- [ ] **Register a GitHub OAuth app** and **enable Device Flow**; copy the
      client id — [GitHub setup](github-setup.md#register-the-oauth-app)
- [ ] **Approve the OAuth app** for your org if it restricts third-party apps —
      [GitHub setup](github-setup.md#organization-approval)
- [ ] **Bake the config** (`client_id`, `owner`, `repo`, `branch`) into
      `config_default.toml` — [Configuration](configuration.md)
- [ ] **(Advanced, only for machines with no Python) build & distribute a frozen
      build** — the default path needs no build; analysts just run `uvx mooring` —
      [Advanced: offline / frozen builds](build-and-distribute.md)
- [ ] **(Optional) Decide on the AI copilot** — whether to enable it: install the
      `copilot` extra, confirm your org's Copilot agent policy is on, and review
      the team-context and PII-guard settings before turning it on for a
      sensitive-data team —
      [Secure AI copilot](ai-privacy.md) · [copilot guide](../users/ai-copilot.md)

!!! tip "The four values you need from GitHub"

    `client_id`, `owner`, `repo`, and `branch`. Where each one comes from — and
    every click to get them — is in [GitHub setup](github-setup.md).

## The two ways to configure a team

The recommended default is the simple PyPI path: analysts run `uvx mooring` and
fill in the runtime setup form once (or you bake the config for them).

| Approach | How analysts get config | Best for |
|----------|-------------------------|----------|
| **Runtime setup form** (recommended) | Analysts run `uvx mooring` and type `client_id` / `owner` / `repo` / `branch` into a one-time form in the hub | Most teams, pilots, mixed repos |
| **Baked** | You edit `config_default.toml` and build a frozen artifact; analysts get a ready-to-use app | Machines with no Python tooling at all |

Both are covered in [Configuration](configuration.md).
