"""Small runtime helpers shared by both presentation adapters (cli and hub).

These live BELOW both adapters so neither has to import the other. The web hub
previously reached into ``cli.py`` for ``SELFTEST_PACKAGES`` and
``workspace_hint`` — a backwards adapter->adapter dependency. Hosting them here
lets cli and hub both depend on a neutral lower module instead.

Pure config/path helpers only: nothing here may import sync, the AI subsystem,
the editor, or either adapter (enforced by .importlinter).
"""

from __future__ import annotations

from mooring import config, paths

# Mooring's own runtime — what the lean bundle must always carry. A repo's
# notebook packages are not listed here: they live in the repo's pyproject.toml
# and are verified per-workspace by pyproject_env.missing_deps().
SELFTEST_PACKAGES = (
    "marimo",
    "requests",
    "truststore",
    "keyring",
    "starlette",
    "uvicorn",
    "platformdirs",
)


def legacy_workspace_hint(cfg: config.Config) -> str:
    """Warn when files live at a past default location but the current default
    doesn't exist yet, so the user can migrate and keep their sync history."""
    if not cfg.repo or cfg.workspace_path:
        return ""
    new = cfg.workspace()
    if (new / ".mooring").is_dir():
        return ""
    for old in paths.legacy_workspaces(cfg.owner or "_", cfg.repo):
        if old != new and (old / ".mooring").is_dir():
            return (
                f"Found an old workspace at {old} — move the folder to {new} "
                "(or set its 'workspace' in the config) to keep your sync history."
            )
    return ""


def workspace_hint(cfg: config.Config) -> str:
    """Combined workspace warnings (legacy location + cloud-sync folder) for the
    hub and selftest, joined into one line."""
    hints = (legacy_workspace_hint(cfg), paths.synced_folder_hint(cfg.workspace()))
    return "  ".join(h for h in hints if h)
