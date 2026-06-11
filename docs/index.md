---
icon: lucide/anchor
---

# ⚓ Mooring

**Git-free [marimo](https://marimo.io) notebook sharing via GitHub.**

Mooring is a single-file app (`mooring.pyz` / `mooring.exe`) that lets a team of
data analysts pull, edit, and push marimo notebooks stored in a shared GitHub
repo — **without git installed on their machines**. All sync happens over the
GitHub REST API; the only requirement on an analyst's machine is Python 3.12.

Double-clicking the app opens a local browser **hub**: log in to GitHub with a
one-time device code, see every team notebook with its sync status, pull the
latest, open notebooks in the bundled marimo editor, and push your changes back
— one commit per file, with conflicts detected and resolved per file (never
silently overwritten).

## How it works

- **One shared team repo** (e.g. `your-org/notebooks`) holds `notebooks/` and
  `data/` folders. Everyone pulls from and pushes to it.
- **No git anywhere.** Pull walks the repo tree via the GitHub Git Data API and
  downloads only changed blobs; push uses the Contents API with the file's
  last-known SHA, so GitHub itself rejects writes that would clobber someone
  else's change.
- **Conflicts are explicit.** Pull never overwrites local edits; push blocks
  conflicted files. The hub offers per-file resolution — see
  [Resolving conflicts](users/conflicts.md).

## Where do I start?

<div class="grid cards" markdown>

-   **I'm an analyst**

    ---

    Install Python, run the app, and pull / edit / push notebooks.

    [For users →](users/index.md)

-   **I'm setting up a team**

    ---

    Create the shared repo, register the GitHub OAuth app, build, and
    distribute the app to your analysts.

    [For admins →](admins/index.md)

-   **I'm working on mooring**

    ---

    Architecture, dev setup, tests, and how the docs are published.

    [For developers →](developers/index.md)

</div>

!!! tip "Looking for the GitHub details?"

    The single most-asked admin question — *where do `client_id`, `owner`,
    `repo`, and the OAuth app come from?* — is answered step by step in
    [GitHub setup](admins/github-setup.md).

---

Built with [moonlit](https://github.com/openafterhours/moonlit).
