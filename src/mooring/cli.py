"""Command-line entry point for mooring."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from mooring import (
    __version__,
    activity,
    config,
    paths,
    pyproject_env,
    shadow,
    telemetry,
    workspace_config,
)

# SELFTEST_PACKAGES, workspace_hint and legacy_workspace_hint now live in
# mooring.runtime — a neutral module below both presentation adapters, so the web
# hub no longer imports the CLI for them (Phase 3 of the architecture migration).
# Re-exported here for back-compat with callers/tests that import them via mooring.cli.
from mooring.runtime import (  # noqa: F401
    SELFTEST_PACKAGES,
    legacy_workspace_hint,
    workspace_hint,
)


_REPO_ARG_HELP = "act on this repo instead of the active one"


def _truststore_disabled(env: Mapping[str, str]) -> bool:
    return env.get("MOORING_TRUSTSTORE", "1").strip().lower() in ("0", "false", "no", "off")


def _inject_truststore(env: Mapping[str, str] | None = None) -> None:
    """Verify TLS against the OS trust store (corporate SSL interception needs
    the proxy's root CA, which IT installs there but not in certifi's bundle)."""
    env = os.environ if env is None else env
    if _truststore_disabled(env):
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as exc:  # noqa: BLE001  # never let TLS setup brick the app
        print(f"Warning: could not enable the OS trust store for TLS: {exc}")


def _ensure_child_pythonpath() -> None:
    """Expose bundled packages to child processes (the marimo server and its kernels).

    moonlit activates its extracted site-packages via site.addsitedir(), which
    subprocesses do not inherit; PYTHONPATH does.
    """
    spec = importlib.util.find_spec("marimo")
    if spec is None or not spec.origin:
        return
    site_dir = str(Path(spec.origin).resolve().parents[1])
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if site_dir not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([site_dir, *parts])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mooring",
        description="Share marimo notebooks via GitHub without git. "
        "Run with no arguments to open the browser hub.",
    )
    parser.add_argument("--version", action="version", version=f"mooring {__version__}")
    sub = parser.add_subparsers(dest="command")

    hub = sub.add_parser("hub", help="open the browser hub (default)")
    hub.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    hub.add_argument("--port", type=int, default=None, help="fixed port for the hub server")

    login = sub.add_parser("login", help="log in to GitHub via device flow")
    login.add_argument(
        "--host",
        default=None,
        help="GitHub host or URL for GitHub Enterprise (e.g. ghe.example.com); "
        "saved as the global host before logging in",
    )
    sub.add_parser("logout", help="forget the stored GitHub token")
    sub.add_parser("whoami", help="show the logged-in GitHub user")
    status = sub.add_parser("status", help="show sync status of workspace files")

    repo = sub.add_parser("repo", help="manage registered team repos")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_sub.add_parser("list", help="list registered repos ('*' marks the active one)")
    repo_add = repo_sub.add_parser("add", help="register a repo and make it active")
    repo_add.add_argument("slug", help="repo as owner/name (e.g. acme/notebooks)")
    repo_add.add_argument("--alias", default=None, help="short name (default: repo name)")
    repo_add.add_argument("--branch", default="main", help="branch to sync (default: main)")
    repo_add.add_argument("--workspace", default="", help="custom local workspace path")
    repo_add.add_argument(
        "--host",
        default=None,
        help="GitHub host or URL for GitHub Enterprise (e.g. ghe.example.com); "
        "stored as the global host",
    )
    repo_add.add_argument("--no-use", action="store_true", help="register without switching to it")
    repo_use = repo_sub.add_parser("use", help="switch the active repo")
    repo_use.add_argument("alias")
    repo_rm = repo_sub.add_parser("remove", help="forget a repo (local files are kept)")
    repo_rm.add_argument(
        "alias", nargs="?", default=None, help="alias to remove (omit when using --all)"
    )
    repo_rm.add_argument(
        "--all", dest="all_repos", action="store_true", help="remove every registered repo"
    )

    pull = sub.add_parser("pull", help="download changes from the team repo")
    pull_grp = pull.add_mutually_exclusive_group()
    pull_grp.add_argument(
        "--theirs", action="store_true", help="overwrite local edits with remote versions"
    )
    pull_grp.add_argument(
        "--keep-both",
        action="store_true",
        help="keep local edits and save remote versions as copies",
    )

    push = sub.add_parser("push", help="upload local changes to the team repo")
    push.add_argument("paths", nargs="*", help="specific files to push (default: all changes)")
    push.add_argument("-m", "--message", default=None, help="commit message")
    push.add_argument(
        "--acknowledge-findings",
        action="store_true",
        help="push files the guard flagged anyway (refused when the team policy is block)",
    )

    propose = sub.add_parser(
        "propose", help="upload changes to a review branch (open a pull request on GitHub)"
    )
    propose.add_argument(
        "paths", nargs="*", help="specific files to propose (default: all changes)"
    )
    propose.add_argument("-m", "--message", default=None, help="commit message")
    propose.add_argument(
        "--acknowledge-findings",
        action="store_true",
        help="propose files the guard flagged anyway (refused when the team policy is block)",
    )

    scan = sub.add_parser(
        "scan", help="scan outgoing changes for secrets/PII/bulk data without pushing"
    )

    recall = sub.add_parser(
        "recall", help="undo your last push on GitHub (history keeps the pushed commit)"
    )
    recall.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    adopt = sub.add_parser(
        "adopt",
        help="sync notebook folders the repo keeps outside the standard synced folders",
    )
    adopt.add_argument(
        "folders",
        nargs="*",
        help="folders to adopt (run with none to list candidates)",
    )
    adopt.add_argument(
        "--all", dest="all_folders", action="store_true", help="adopt every candidate folder"
    )

    open_cmd = sub.add_parser("open", help="open a notebook in the marimo editor")
    open_cmd.add_argument("path", help="workspace-relative notebook path")

    new = sub.add_parser("new", help="create a new notebook and open it")
    new.add_argument(
        "name",
        help="notebook name or path (e.g. sales-analysis, or "
        "packages/finance/notebooks/sales)",
    )

    dup = sub.add_parser(
        "duplicate",
        help="copy a notebook to a personal -draft sibling and open it",
    )
    dup.add_argument("path", help="workspace-relative notebook path to duplicate")

    delete_cmd = sub.add_parser(
        "delete",
        help="delete a notebook from the workspace (push afterwards to remove it remotely)",
    )
    delete_cmd.add_argument(
        "path", help="workspace-relative notebook path (a .py file or a .pbip project)"
    )
    delete_cmd.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    rollback_cmd = sub.add_parser(
        "rollback",
        help="discard local changes to a notebook and restore the last synced version (needs login)",
    )
    rollback_cmd.add_argument("path", help="workspace-relative notebook path to revert")
    rollback_cmd.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    rollback_cmd.add_argument(
        "--conflicts",
        action="store_true",
        help="also discard your edit to a conflicted file (turns it into a clean pull)",
    )

    history_cmd = sub.add_parser(
        "history", help="list a file's pushed versions from the team repo (needs login)"
    )
    history_cmd.add_argument("path", help="workspace-relative file path")
    history_cmd.add_argument("--page", type=int, default=1, help="older pages (30 per page)")

    sub.add_parser(
        "whatsnew",
        help="who changed what on the team branch since your last sync (needs login)",
    )

    restore_cmd = sub.add_parser(
        "restore", help="bring back a file as it was at a past version (needs login)"
    )
    restore_cmd.add_argument("path", help="workspace-relative file path")
    restore_cmd.add_argument(
        "--at", required=True, metavar="SHA", help="the version's commit sha (see `mooring history`)"
    )
    restore_cmd.add_argument(
        "--copy",
        action="store_true",
        help="write it beside the file as {name}.restored-{sha7} instead of overwriting",
    )
    restore_cmd.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )

    trash_cmd = sub.add_parser(
        "trash", help="list and restore local pre-images saved before destructive actions"
    )
    trash_sub = trash_cmd.add_subparsers(dest="trash_command", required=True)
    trash_sub.add_parser("list", help="list saved pre-images, newest first")
    trash_restore = trash_sub.add_parser(
        "restore", help="restore one saved pre-image to its original path"
    )
    trash_restore.add_argument("token", help="the entry token (from `mooring trash list`)")

    activity_cmd = sub.add_parser(
        "activity", help="show what mooring did in this workspace (local journal)"
    )
    activity_cmd.add_argument(
        "--path", default=None, help="only entries touching this workspace-relative path"
    )
    activity_cmd.add_argument(
        "--limit", type=int, default=50, help="how many entries to show (default 50)"
    )

    init_cmd = sub.add_parser(
        "init",
        help="create the repo's pyproject.toml (its notebook dependencies) and lock it",
    )

    deps = sub.add_parser("deps", help="manage the repo's notebook dependencies")
    deps_sub = deps.add_subparsers(dest="deps_command", required=True)
    deps_add = deps_sub.add_parser("add", help="add packages to the repo and re-lock")
    deps_add.add_argument("packages", nargs="+", help="packages to add (e.g. polars 'scipy>=1.11')")
    deps_rm = deps_sub.add_parser("remove", help="remove packages from the repo and re-lock")
    deps_rm.add_argument("packages", nargs="+", help="packages to remove")
    deps_sub.add_parser("list", help="list declared packages and whether each is available")
    deps_sub.add_parser("lock", help="refresh uv.lock from pyproject.toml")

    build_reqs = sub.add_parser(
        "build-requirements",
        help="export the repo's pinned deps for a frozen build (see docs: build & distribute)",
    )
    build_reqs.add_argument(
        "-o", "--output", default=None, help="write to this file (default: stdout)"
    )

    for cmd in (
        status,
        pull,
        push,
        propose,
        scan,
        recall,
        adopt,
        open_cmd,
        new,
        delete_cmd,
        rollback_cmd,
        history_cmd,
        restore_cmd,
        trash_cmd,
        activity_cmd,
        init_cmd,
        deps,
        build_reqs,
    ):
        cmd.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)

    shadow_cmd = sub.add_parser("shadow", help="manage the notebook-name shadow guard")
    shadow_sub = shadow_cmd.add_subparsers(dest="shadow_command", required=True)
    shadow_ignore = shadow_sub.add_parser(
        "ignore", help="silence the shadow warning for one notebook (it travels to teammates)"
    )
    shadow_ignore.add_argument("path", help="workspace-relative notebook path")
    shadow_ignore.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)
    shadow_unignore = shadow_sub.add_parser(
        "unignore", help="re-enable the shadow warning for one notebook"
    )
    shadow_unignore.add_argument("path", help="workspace-relative notebook path")
    shadow_unignore.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)

    ai = sub.add_parser("ai", help="AI copilot: sign in to Copilot and check status")
    ai_sub = ai.add_subparsers(dest="ai_command", required=True)
    ai_sub.add_parser("status", help="show the AI provider's sign-in status")
    ai_login = ai_sub.add_parser("login", help="sign in to Copilot (OAuth device flow)")
    ai_login.add_argument(
        "--host", default=None, help="GitHub host URL for Copilot (GHE data residency)"
    )
    ai_dict = ai_sub.add_parser(
        "dictionary", help="inspect how the team data dictionary (context/) parses"
    )
    ai_dict_sub = ai_dict.add_subparsers(dest="ai_dict_command", required=True)
    ai_dict_check = ai_dict_sub.add_parser(
        "check", help="parse context/ dictionaries and report tables, columns, and dropped keys"
    )
    ai_dict_check.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)
    # `ai model check` (this Power BI semantic-model lint) is a DIFFERENT command
    # from `ai pii model` below (the NER model download) — one level up the tree.
    ai_model = ai_sub.add_parser(
        "model", help="inspect what the copilot would see of a Power BI semantic model"
    )
    ai_model_sub = ai_model.add_subparsers(dest="ai_model_command", required=True)
    ai_model_check = ai_model_sub.add_parser(
        "check",
        help="parse synced .SemanticModel folders and report what's kept, excluded, and flagged",
    )
    ai_model_check.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)
    ai_pii = ai_sub.add_parser(
        "pii", help="scan context/ and notebook source for structured-PII risks (offline)"
    )
    ai_pii_sub = ai_pii.add_subparsers(dest="ai_pii_command", required=True)
    ai_pii_check = ai_pii_sub.add_parser(
        "check", help="scan instructions, dictionaries, and notebooks for PII shapes"
    )
    ai_pii_check.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)
    ai_pii_check.add_argument(
        "--notebook",
        default=None,
        metavar="REL",
        help="also scan a single notebook (workspace-relative)",
    )
    ai_pii_model = ai_pii_sub.add_parser(
        "model", help="download/verify the local NER name-detection model (needs the pii extra)"
    )
    ai_pii_model.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)
    ai_pii_sub.add_parser(
        "doctor",
        help="check the PII guard config end-to-end: which backend runs, what's ready, what to fix",
    )
    ai_tb = ai_sub.add_parser(
        "traceback", help="preview the value-safe traceback rewrite (offline)"
    )
    ai_tb_sub = ai_tb.add_subparsers(dest="ai_traceback_command", required=True)
    ai_tb_check = ai_tb_sub.add_parser(
        "check", help="sanitise a pasted traceback from FILE (or stdin) and print the rewrite"
    )
    ai_tb_check.add_argument(
        "file",
        nargs="?",
        default=None,
        metavar="FILE",
        help="file containing the traceback (omit to read from stdin)",
    )
    ai_tb_check.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)

    cfg_cmd = sub.add_parser("config", help="view and edit settings in your user config.toml")
    cfg_sub = cfg_cmd.add_subparsers(dest="config_command", required=True)
    cfg_set = cfg_sub.add_parser(
        "set", help="set a dotted key, e.g. `config set ai.pii.enabled true`"
    )
    cfg_set.add_argument("key", help="dotted setting name, e.g. ai.pii.detect_names")
    cfg_set.add_argument(
        "value",
        nargs="+",
        help="value: true/false, a number, or text; give several tokens for a list "
        "(e.g. `... name_labels person name organization`)",
    )
    cfg_get = cfg_sub.add_parser("get", help="print the effective value of a dotted key")
    cfg_get.add_argument("key", help="dotted setting name, e.g. ai.pii.enabled")
    cfg_unset = cfg_sub.add_parser("unset", help="remove a dotted key (revert it to the default)")
    cfg_unset.add_argument("key", help="dotted setting name, e.g. ai.pii.enabled")
    cfg_sub.add_parser("list", help="print the effective merged configuration")
    cfg_sub.add_parser("path", help="print the path to your user config.toml")

    doctor_cmd = sub.add_parser(
        "doctor", help="diagnose the setup in plain English (network, login, config, deps)"
    )
    doctor_cmd.add_argument(
        "--report",
        action="store_true",
        help="print only the paste-safe report (redacted; safe for a support ticket)",
    )
    doctor_cmd.add_argument("--repo", default=None, metavar="ALIAS", help=_REPO_ARG_HELP)

    sub.add_parser("selftest", help="verify the bundled environment")
    sub.add_parser("version", help="print the version")
    return parser


def _print_paths(cfg: config.Config) -> None:
    print(f"  config file : {paths.user_config_file()}")
    print(f"  workspace   : {cfg.workspace()}")
    print(f"  logs        : {paths.user_log_dir()}")
    hints = (legacy_workspace_hint(cfg), paths.synced_folder_hint(cfg.workspace()))
    for hint in (h for h in hints if h):
        print(f"  note        : {hint}")


def copilot_probe_for(app_cfg: config.AppConfig):
    """The adapter-appended Copilot probe (ai/ sits above doctor.py, so the
    engine can't run this itself). Force-checks the provider — slow (spawns the
    Copilot CLI), which is fine for an on-demand health check."""
    from mooring import doctor

    def probe() -> doctor.ProbeResult:
        try:
            from mooring.ai import get_provider

            st = get_provider(app_cfg).status(force=True)
        except Exception:  # noqa: BLE001  # a probe never raises; unknown is honest
            return doctor.ProbeResult(
                "copilot", "AI copilot", doctor.UNKNOWN,
                "Copilot could not be checked.",
                "Run `mooring ai status` for details (needs the mooring[copilot] extra).",
            )
        if not st.available:
            return doctor.ProbeResult(
                "copilot", "AI copilot", doctor.WARN,
                "Copilot isn't available in this build.",
                "Install the mooring[copilot] extra, or ask your admin to include it.",
            )
        if not st.connected:
            return doctor.ProbeResult(
                "copilot", "AI copilot", doctor.WARN,
                "Copilot is installed but not signed in.",
                "Sign in with `mooring ai login` (or the hub's Copilot menu). "
                "For the PII guard, see `mooring ai pii doctor`.",
            )
        detail = f"Connected as @{st.account}." if st.account else "Connected."
        return doctor.ProbeResult("copilot", "AI copilot", doctor.PASS, detail)

    return probe


def cmd_doctor(app_cfg: config.AppConfig, cfg: config.Config, report_only: bool) -> int:
    from mooring import doctor

    extra = [copilot_probe_for(app_cfg)] if app_cfg.ai_enabled else []
    results = doctor.run_probes(cfg, extra_probes=extra)
    counts = {
        s: sum(1 for r in results if r.status == s)
        for s in (doctor.PASS, doctor.WARN, doctor.FAIL, doctor.UNKNOWN)
    }
    telemetry.log_event("doctor", **counts)
    if report_only:
        print(doctor.build_report(results, cfg), end="")
    else:
        print(f"mooring doctor (mooring {__version__}):\n")
        for line in doctor.render_lines(results):
            print(line)
        print(
            f"\n{counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail, "
            f"{counts['unknown']} unknown."
        )
        print("Paste-safe report for a ticket: mooring doctor --report")
    return 1 if counts[doctor.FAIL] else 0


def cmd_selftest(app_cfg: config.AppConfig, cfg: config.Config) -> int:
    import importlib.metadata

    print(f"mooring {__version__}  (python {sys.version.split()[0]}, {sys.executable})")
    failures = []
    for name in SELFTEST_PACKAGES:
        try:
            importlib.import_module(name)
            version = importlib.metadata.version(name)
            print(f"  ok  {name} {version}")
        except Exception as exc:  # noqa: BLE001  # report and continue
            failures.append(name)
            print(f"  FAIL {name}: {exc}")
    _print_paths(cfg)
    print(f"  PYTHONPATH  : {os.environ.get('PYTHONPATH', '(not set)')}")
    tls = (
        "disabled via MOORING_TRUSTSTORE=0"
        if _truststore_disabled(os.environ)
        else "OS trust store (truststore)"
    )
    print(f"  tls trust   : {tls}")
    log_dest = app_cfg.log_endpoint.strip()
    if log_dest:
        kind = "url" if log_dest.lower().startswith(("http://", "https://")) else "path"
        print(f"  logging     : on -> {log_dest} ({kind})")
    else:
        print("  logging     : off (no endpoint configured)")
    if cfg.is_configured:
        print(f"  team repo   : {cfg.repo_slug} (branch {cfg.branch}, host {cfg.host})")
    else:
        print("  team repo   : not configured")
    if failures:
        print(f"selftest FAILED: {', '.join(failures)}")
        return 1
    print("selftest OK")
    return 0


def _require_token(cfg: config.Config) -> str:
    from mooring import auth

    token = auth.get_token(host=cfg.host)
    if not token:
        sys.exit("Not logged in. Run `mooring login` first.")
    return token


def _client(cfg: config.Config):
    # Shared construction (app/notebooks) RAISES; the CLI owns the exit + guidance.
    from mooring.app import notebooks
    from mooring.github import AuthFailed

    try:
        return notebooks.client_for(cfg)
    except notebooks.NotConfigured:
        sys.exit(
            "No team repo configured. Set [github] owner/repo/client_id in "
            f"{paths.user_config_file()} (or run the hub for guided setup)."
        )
    except AuthFailed:
        sys.exit("Not logged in. Run `mooring login` first.")


def cmd_login(cfg: config.Config, host: str | None = None) -> int:
    import requests

    from mooring import auth, config_store

    if host is not None:
        try:
            new_host = config_store.set_host(host)
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"Saved GitHub host: {new_host}")
        cfg = config.load_config()  # pick up the host just written
    if not cfg.client_id:
        sys.exit(
            f"No OAuth client_id configured. Set [github] client_id in {paths.user_config_file()}."
        )
    print(f"Requesting device code from {cfg.host}…")
    try:
        device = auth.start_device_flow(cfg.client_id, host=cfg.host)
    except (auth.AuthError, requests.RequestException) as exc:
        sys.exit(auth.device_flow_hint(cfg.host, exc))
    print(f"Open {device.verification_uri} and enter code: {device.user_code}")
    print("Waiting for authorization...")
    token = auth.poll_for_token(cfg.client_id, device)
    auth.save_token(token, host=cfg.host)
    from mooring.github import GitHubClient

    user = GitHubClient(token, cfg.owner, cfg.repo, host=cfg.host).get_user()
    telemetry.set_user(user["login"])
    telemetry.log_event("login")
    print(f"Logged in as {user['login']}.")
    return 0


def cmd_logout(cfg: config.Config) -> int:
    from mooring import auth

    auth.delete_token(host=cfg.host)
    telemetry.log_event("logout")
    print("Logged out.")
    return 0


def cmd_whoami(cfg: config.Config) -> int:
    from mooring.github import GitHubClient

    user = GitHubClient(_require_token(cfg), cfg.owner, cfg.repo, host=cfg.host).get_user()
    telemetry.set_user(user["login"])
    telemetry.log_event("whoami")
    print(user["login"])
    return 0


def cmd_status(cfg: config.Config) -> int:
    from mooring import sync
    from mooring.github import Unreachable

    client = _client(cfg)
    try:
        report = sync.status(client, cfg)
    except Unreachable as exc:
        # Offline: show the last observed sync state (sync.cached_status — pure
        # local reads) under a loud header instead of a traceback. The adopt
        # hint needs a live tree read, so it is skipped here. No cache yet →
        # nothing local to show, so exit with the classified one-liner.
        cached = sync.cached_status(cfg)
        if cached is None:
            sys.exit(str(exc))
        report, fetched_at = cached
        print(f"OFFLINE — GitHub unreachable; showing sync state as of {fetched_at}")
        if not report.files:
            print("Workspace empty and no remote files. Try `mooring new <name>`.")
            return 0
        return _print_status_report(cfg, report)
    if not report.files:
        # An EMPTY in-scope report is exactly the headline case (a new repo whose
        # notebooks all live outside the synced folders): run discovery so the hint can
        # replace the misleading "workspace empty" line. Scoped to this branch so a
        # normal status (with in-scope files) keeps sync's no-tree-fetch fast path —
        # discovery needs the full tree, which would otherwise defeat it on every call.
        hint = _adopt_hint_lines(client, cfg, report.head_commit)
        for line in hint or ["Workspace empty and no remote files. Try `mooring new <name>`."]:
            print(line)
        return 0
    return _print_status_report(cfg, report)


def _print_status_report(cfg: config.Config, report) -> int:
    """The width-aligned rows + summary shared by the live and OFFLINE status
    paths (the shadow warnings are local, so they print in both)."""
    width = max(len(f.path) for f in report.files)
    for f in report.files:
        print(f"  {f.path:<{width}}  {f.state.value}")
    print(report.summary())
    if report.review_branch:
        print(f"proposal open on {report.review_branch}")
    for line in _shadow_warning_lines(cfg, [f.path for f in report.files]):
        print(line)
    return 0


def _adopt_hint_lines(client, cfg: config.Config, head: str) -> list[str]:
    """Best-effort 'you have notebook folders outside the synced scope' hint for
    ``status``. Reuses the head ``status`` already fetched so it costs one tree read,
    and never breaks ``status`` — any discovery failure simply yields no hint."""
    from mooring import sync
    from mooring.github import GitHubError

    try:
        candidates = sync.discover_unsynced_folders(client, cfg, head=head or None)
    except (GitHubError, OSError):
        return []
    if not candidates:
        return []
    shown = ", ".join(c.folder for c in candidates[:6])
    more = "" if len(candidates) <= 6 else f", +{len(candidates) - 6} more"
    return [
        f"note: {len(candidates)} folder(s) outside the synced folders hold files: {shown}{more}",
        "  Run `mooring adopt` to sync the ones with your notebooks (and their helper modules).",
    ]


def cmd_pull(cfg: config.Config, theirs: bool, keep_both: bool) -> int:
    from mooring import sync

    strategy = (
        sync.ConflictStrategy.THEIRS
        if theirs
        else sync.ConflictStrategy.KEEP_BOTH
        if keep_both
        else sync.ConflictStrategy.SKIP
    )
    result = sync.pull(_client(cfg), cfg, strategy=strategy)
    telemetry.log_event(
        "pull",
        pulled=result.pulled,
        conflicts=len(result.skipped_conflicts),
        lines=len(result.lines),
        strategy=strategy.value,
    )
    _record_activity(cfg, "pull", result)
    _print_sync_result(result)
    return 0 if not result.skipped_conflicts else 1


def _push_guard_fn(cfg: config.Config, acknowledge: bool):
    """The push guard for a CLI push/propose. Returns
    ``(guard_fn, collected, mode, acknowledged)``.

    ``--acknowledge-findings`` does NOT turn the scan off: the guard still runs
    and everything it would have flagged is collected into ``acknowledged`` and
    printed after the push — so the user sees exactly what they let out AT PUSH
    TIME, and a finding that appeared since the first run can't ride out unseen
    (the hub's token flow gets the same guarantee by binding tokens to bytes).
    In block mode ([guard] push = "block") the flag is refused entirely."""
    from mooring import pushguard

    mode = workspace_config.guard_mode(cfg.workspace())
    if acknowledge and mode != "block":
        acknowledged: dict = {}

        def allow_fn(rel_path: str, data: bytes) -> list[str]:
            findings = pushguard.scan_text(rel_path, data)
            if findings:
                acknowledged[rel_path] = {"findings": findings}
            return []

        return allow_fn, {}, mode, acknowledged
    guard_fn, collected = pushguard.make_guard()
    return guard_fn, collected, mode, {}


def _print_guard_findings(collected: dict, mode: str, verb: str) -> None:
    print(f"\n{len(collected)} file(s) withheld — they contain something that looks sensitive:")
    for path, info in sorted(collected.items()):
        for f in info["findings"]:
            print(f"  {path}:{f.line}  {f.kind}")
    if mode == "block":
        print(
            "Your team's policy blocks pushing flagged files ([guard] push = \"block\").\n"
            "Remove the flagged content, or add a `mooring: push-ok` comment on a\n"
            f"reviewed false-positive line, then {verb} again."
        )
    else:
        print(
            "Remove the flagged content, add a `mooring: push-ok` comment on a reviewed\n"
            f"false-positive line, or re-run with --acknowledge-findings to {verb} anyway."
        )


def _print_acknowledged(acknowledged: dict) -> None:
    total = sum(len(info["findings"]) for info in acknowledged.values())
    print(f"\nPushed with {total} acknowledged finding(s) — now visible to everyone:")
    for path, info in sorted(acknowledged.items()):
        for f in info["findings"]:
            print(f"  {path}:{f.line}  {f.kind}")


def cmd_push(
    cfg: config.Config, only_paths: list[str], message: str | None, acknowledge: bool = False
) -> int:
    from mooring import sync

    guard_fn, collected, mode, acknowledged = _push_guard_fn(cfg, acknowledge)
    result = sync.push(
        _client(cfg), cfg, paths=only_paths or None, message=message, guard_fn=guard_fn
    )
    telemetry.log_event(
        "push",
        pushed=result.pushed,
        conflicts=len(result.blocked_conflicts),
        lines=len(result.lines),
        withheld=len(collected),
    )
    _record_activity(cfg, "push", result)
    _print_sync_result(result)
    if collected:
        _print_guard_findings(collected, mode, "push")
    if acknowledged:
        _print_acknowledged(acknowledged)
    return 0 if not (result.blocked_conflicts or collected) else 1


def cmd_propose(
    cfg: config.Config, only_paths: list[str], message: str | None, acknowledge: bool = False
) -> int:
    from mooring import sync

    guard_fn, collected, mode, acknowledged = _push_guard_fn(cfg, acknowledge)
    result = sync.propose(
        _client(cfg), cfg, paths=only_paths or None, message=message, guard_fn=guard_fn
    )
    telemetry.log_event(
        "propose",
        proposed=result.proposed,
        conflicts=len(result.blocked_conflicts),
        review_branch=bool(result.review_branch),
        withheld=len(collected),
    )
    _record_activity(cfg, "propose", result)
    _print_sync_result(result)
    if collected:
        _print_guard_findings(collected, mode, "propose")
    if acknowledged:
        _print_acknowledged(acknowledged)
    return 0 if not (result.blocked_conflicts or collected) else 1


def cmd_scan(cfg: config.Config) -> int:
    """Run the push guard over the current push candidates without pushing —
    the push-scoped sibling of `mooring ai pii check`."""
    from mooring import pushguard, sync

    report = sync.status(_client(cfg), cfg)
    workspace = cfg.workspace()
    findings_total = 0
    for f in report.by_state(*sync.PUSH_STATES):
        target = workspace / f.path
        if not target.is_file():
            continue  # a deletion has no bytes to scan
        findings = pushguard.scan_text(f.path, target.read_bytes())
        for finding in findings:
            print(f"  {f.path}:{finding.line}  {finding.kind}")
        findings_total += len(findings)
    if findings_total:
        print(
            f"{findings_total} finding(s). Fix them, or mark a reviewed false positive "
            "with a `mooring: push-ok` comment on that line."
        )
        return 1
    print("No findings in the current push candidates.")
    return 0


def cmd_recall(cfg: config.Config, assume_yes: bool) -> int:
    from mooring import manifest as manifest_mod, sync

    recorded = sorted(manifest_mod.load(cfg.workspace()).last_push)
    if not assume_yes:
        if not sys.stdin.isatty():
            sys.exit("Refusing to recall the last push without confirmation. Re-run with --yes.")
        # Name exactly what would be reverted — the way a stale record gets caught.
        for rel in recorded[:12]:
            print(f"  would revert {rel}")
        if len(recorded) > 12:
            print(f"  …and {len(recorded) - 12} more")
        prompt = (
            "Undo your last push on GitHub? The previous versions are written back; "
            "the pushed commit stays in history. [y/N] "
        )
        if input(prompt).strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0
    result = sync.recall(_client(cfg), cfg)
    telemetry.log_event("recall", recalled=result.pushed, conflicts=len(result.blocked_conflicts))
    _record_activity(cfg, "recall", result)
    _print_sync_result(result)
    return 0 if not result.blocked_conflicts else 1


def cmd_adopt(cfg: config.Config, folders: list[str], all_folders: bool) -> int:
    """Register repo folders that hold notebooks outside the standard synced folders
    into the synced ``mooring.toml`` ``[sync] folders``, then pull them.

    With no folders (and no ``--all``) it lists the candidates and exits. The chosen
    folders are validated against what discovery actually found, so adopt never
    registers a typo or a non-existent folder. The registration is written to the
    SYNCED ``mooring.toml`` (via :func:`mooring.workspace_config.add_extra_folder`), so
    pushing it shares the new scope with the whole team.
    """
    from mooring import sync
    from mooring.github import GitHubError

    client = _client(cfg)
    try:
        candidates = sync.discover_unsynced_folders(client, cfg)
    except GitHubError as exc:
        sys.exit(str(exc))
    if not candidates:
        print("No folders outside the synced folders. Nothing to adopt.")
        return 0
    if not folders and not all_folders:
        print("Folders with files outside the synced folders:")
        width = max(len(c.folder) for c in candidates)
        for c in candidates:
            print(f"  {c.folder:<{width}}  {c.py_files} Python file(s), {c.files} file(s) total")
        print("\nAdopt one or more: `mooring adopt <folder> [...]` (or `mooring adopt --all`).")
        print(
            f"Adopted folders are saved to the repo's {workspace_config.WORKSPACE_CONFIG_NAME} "
            "and pulled; push it to share them with the team."
        )
        return 0

    from mooring.app import notebooks

    if all_folders:
        chosen = sorted(c.folder for c in candidates)
    else:
        # Unlike the hub (which silently adopts the valid subset), the CLI refuses
        # the whole command when any requested folder isn't adoptable.
        chosen, unknown = notebooks.resolve_adoptable(candidates, folders)
        if unknown:
            sys.exit(
                f"Not adoptable: {', '.join(unknown)}. "
                "Run `mooring adopt` (no arguments) to see the candidates."
            )

    import tomllib

    try:
        result = notebooks.adopt_folders(client, cfg, chosen)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(
            f"Can't update {workspace_config.WORKSPACE_CONFIG_NAME}: it is not valid TOML "
            f"({exc}). Fix it (pull a teammate's version, or repair it) and retry."
        )
    telemetry.log_event("adopt", folders=len(chosen), pulled=result.pulled)
    print(f"Adopted: {', '.join(chosen)} (saved to {workspace_config.WORKSPACE_CONFIG_NAME}).")
    for line in result.lines:
        print(f"  {line}")
    print(result.summary())
    print(
        f"Run `mooring push` to share {workspace_config.WORKSPACE_CONFIG_NAME} so teammates "
        "pull these folders too."
    )
    return 0 if not result.skipped_conflicts else 1


def cmd_open(cfg: config.Config, rel_path: str) -> int:
    import webbrowser

    from mooring.editor import EditorServer

    workspace = cfg.workspace()
    target = workspace / rel_path
    if not target.is_file():
        sys.exit(f"No such notebook: {target}")
    # The gate (pbip / .py-only / module-refusal) is shared policy in app/notebooks.
    from mooring.app import notebooks

    try:
        kind = notebooks.openable_kind(target, rel_path, display=rel_path)
    except notebooks.OpenRefused as exc:
        sys.exit(str(exc))
    if kind == "pbip":
        from mooring import pbip

        try:
            pbip.launch(target)
        except pbip.PbipLaunchError as exc:
            sys.exit(str(exc))
        telemetry.log_event("open", kind="pbip")
        print(f"Opened {rel_path} in Power BI Desktop.")
        return 0
    server = EditorServer(workspace)
    # Ungated by the launch backend: the sys.path[0] shadow trap is plain Python
    # import resolution and bites uv and frozen runs alike, unlike the missing-deps
    # note below (which is a bundle-only concern). Scans the whole folder, so opening
    # an innocent notebook still warns when a sibling poisons the directory.
    if cfg.warn_shadowed_notebooks:
        for line in shadow.warning_lines(notebooks.open_shadow_findings(workspace, rel_path)):
            print(line)
    if not server.use_uv():
        for line in _missing_deps_lines(pyproject_env.missing_deps(workspace)):
            print(line)
    server.ensure_started()
    url = server.url_for(rel_path)
    telemetry.log_event("open", kind="notebook", uv=server.use_uv())
    print(f"Editor running at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def _missing_deps_lines(missing: list[str]) -> list[str]:
    if not missing:
        return []
    return [
        f"Warning: this build can't provide: {', '.join(missing)}",
        f"  Declared in {pyproject_env.PYPROJECT_NAME} but not in the bundle — importing "
        "them will fail.",
        "  Ask your admin to include them in the build, or run mooring via uv.",
    ]


def _shadow_warning_lines(cfg: config.Config, rel_paths: list[str]) -> list[str]:
    from mooring.app import notebooks

    if not cfg.warn_shadowed_notebooks:
        return []
    workspace = cfg.workspace()
    extra, ignore = notebooks.shadow_policy(workspace)
    # Include the workspace-root .py files: the root is on every notebook's sys.path
    # (runtime.pythonpath), so a root-level shadow is globally dangerous, but root files
    # aren't in the synced `rel_paths` list. Merged by path.
    findings = {
        **shadow.root_shadows(workspace, extra=extra, ignore=ignore),
        **shadow.scan(rel_paths, workspace=workspace, extra=extra, ignore=ignore),
    }
    return shadow.warning_lines(findings)


def cmd_shadow(cfg: config.Config, args: argparse.Namespace) -> int:
    workspace = cfg.workspace()
    rel = workspace_config.normalize_notebook(args.path)
    ignoring = args.shadow_command == "ignore"
    workspace_config.set_shadow_ignored(workspace, rel, ignoring)
    telemetry.log_event("shadow", action=args.shadow_command)
    print(f"{rel} is {'now ignored by' if ignoring else 'no longer ignored by'} the shadow guard.")
    return 0


def cmd_new(cfg: config.Config, name: str) -> int:
    from mooring import notebook_template

    workspace = cfg.workspace()
    if pyproject_env.scaffold(workspace):
        print(f"Created {pyproject_env.PYPROJECT_NAME} for this repo's notebook dependencies.")
    try:
        rel_path = notebook_template.create_from_input(
            workspace, name, folders=cfg.folders, exclude=cfg.exclude
        )
    except (ValueError, FileExistsError) as exc:
        sys.exit(str(exc))
    telemetry.log_event("new")
    print(f"Created {rel_path}")
    return cmd_open(cfg, rel_path)


def cmd_duplicate(cfg: config.Config, rel_path: str) -> int:
    from mooring import notebook_template
    from mooring.app import notebooks
    from mooring.github import AuthFailed, GitHubError

    # The owner suffix is the GitHub login; no repo (NotConfigured subclasses
    # AuthFailed), no login, or an offline machine degrades to plain "-draft"
    # rather than failing the copy — the draft is a purely local file.
    try:
        owner = notebooks.client_for(cfg).get_user()["login"]
    except (AuthFailed, GitHubError, OSError):
        owner = ""
    try:
        new_rel = notebook_template.duplicate_as_draft(
            cfg.workspace(), rel_path, owner=owner, exclude=cfg.exclude
        )
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        sys.exit(str(exc))
    telemetry.log_event("duplicate")
    _record_activity(cfg, "duplicate", path=rel_path, draft=new_rel)
    print(f"Created {new_rel}")
    return cmd_open(cfg, new_rel)


def cmd_init(cfg: config.Config) -> int:
    workspace = cfg.workspace()
    target = pyproject_env.pyproject_path(workspace)
    if not pyproject_env.scaffold(workspace):
        print(f"{pyproject_env.PYPROJECT_NAME} already exists at {target}.")
        return 0
    telemetry.log_event("init")
    print(f"Created {target} (marimo only — add your team's packages).")
    if pyproject_env.lock_path(workspace).is_file():
        print(f"Locked {pyproject_env.LOCK_NAME}.")
    elif not pyproject_env.uv_available():
        print("Install uv to generate uv.lock (`mooring deps lock`), or commit just the pyproject.")
    print("Next: `mooring deps add <pkg>` to add packages, then `mooring push` to share.")
    return 0


def cmd_deps(cfg: config.Config, args: argparse.Namespace) -> int:
    workspace = cfg.workspace()
    command = args.deps_command
    if command == "list":
        status = pyproject_env.dep_status(workspace)
        if not status:
            if not pyproject_env.has_pyproject(workspace):
                print("No pyproject.toml yet. Run `mooring init`.")
            else:
                print("No dependencies declared.")
            return 0
        for req, available in status:
            print(f"  {'ok' if available else 'missing':<8} {req}")
        unavailable = [r for r, ok in status if not ok]
        if unavailable:
            print(f"{len(unavailable)} not available in this environment.")
        return 0
    if command in ("remove", "lock") and not pyproject_env.has_pyproject(workspace):
        sys.exit("No pyproject.toml yet. Run `mooring init` first.")
    try:
        if command == "add":
            pyproject_env.scaffold(workspace, name=cfg.repo or None, lock=False)
            pyproject_env.add(workspace, args.packages)
            print(f"Added: {', '.join(args.packages)}. Run `mooring push` to share.")
        elif command == "remove":
            pyproject_env.remove(workspace, args.packages)
            print(f"Removed: {', '.join(args.packages)}. Run `mooring push` to share.")
        elif command == "lock":
            pyproject_env.run_lock(workspace)
            print(f"Locked {pyproject_env.LOCK_NAME}.")
    except pyproject_env.UvNotAvailable as exc:
        sys.exit(str(exc))
    except subprocess.CalledProcessError as exc:
        sys.exit(f"uv {command} failed (exit {exc.returncode}).")
    telemetry.log_event("deps", action=command)
    return 0


def cmd_build_requirements(cfg: config.Config, output: str | None) -> int:
    workspace = cfg.workspace()
    if not pyproject_env.has_pyproject(workspace):
        sys.exit("No pyproject.toml in the workspace. Run `mooring init` first.")
    text = pyproject_env.export_requirements(workspace)
    telemetry.log_event("build_requirements")
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"Wrote {output}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_delete(cfg: config.Config, rel_path: str, assume_yes: bool) -> int:
    from mooring import deletion

    workspace = cfg.workspace()
    try:
        targets = deletion.target_paths(workspace, rel_path, cfg.exclude, cfg.folders)
    except ValueError as exc:
        sys.exit(str(exc))
    existing = [t for t in targets if (workspace / t).is_file()]
    if not existing:
        sys.exit(f"No such notebook: {workspace / rel_path}")
    if not assume_yes:
        count = len(existing)
        what = rel_path if count == 1 else f"{rel_path} ({count} files)"
        if not sys.stdin.isatty():
            sys.exit(f"Refusing to delete {what} without confirmation. Re-run with --yes.")
        if input(f"Delete {what} from the workspace? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0
    trashed: list[dict] = []
    try:
        removed = deletion.delete(
            workspace,
            rel_path,
            cfg.exclude,
            cfg.folders,
            trash_cap_mb=cfg.trash_max_file_mb,
            on_trash=lambda rel, token: trashed.append({"path": rel, "token": token}),
        )
    except FileNotFoundError:  # vanished between the prompt and the delete
        sys.exit(f"No such notebook: {workspace / rel_path}")
    telemetry.log_event("delete", count=len(removed))
    _record_activity(cfg, "delete", path=rel_path, paths=removed, trashed=trashed)
    for r in removed:
        print(f"  deleted {r}")
    if trashed:
        print("  (saved to the trash — `mooring trash list` to restore)")
    print(
        f"Deleted {rel_path} locally. Run `mooring push` (or `propose`) to remove it "
        "from the team repo."
    )
    return 0


def _print_sync_result(result) -> None:
    for line in result.lines:
        print(f"  {line}")
    if result.trashed:
        print(
            f"  ({len(result.trashed)} overwritten/removed local file(s) saved to the trash — "
            "`mooring trash list` to see them)"
        )
    print(result.summary())


def _record_activity(cfg: config.Config, op: str, result=None, **fields) -> None:
    """Append to the workspace's LOCAL activity ledger (see mooring.activity) —
    the same journal the hub writes; strictly local, distinct from telemetry."""
    if result is not None:
        fields.setdefault("summary", result.summary())
        fields.setdefault("lines", result.lines[:20])
        fields.setdefault("trashed", [{"path": p, "token": t} for p, t in result.trashed])
    activity.record(cfg.workspace(), op, **fields)


def cmd_history(cfg: config.Config, rel_path: str, page: int) -> int:
    from mooring import sync

    versions = sync.history(_client(cfg), cfg, rel_path, page=max(1, page))
    if not versions:
        print(
            f"No pushed versions found for {rel_path}"
            + ("" if page <= 1 else f" on page {page}")
            + "."
        )
        return 0
    for v in versions:
        who = f"  {v['author']}" if v["author"] else ""
        print(f"  {v['short']}  {v['date']}{who}  {v['message']}")
    if len(versions) == 30:
        print(f"  (older versions: mooring history {rel_path} --page {page + 1})")
    print(f"Restore one with: mooring restore {rel_path} --at <sha>  (add --copy to keep both)")
    return 0


def cmd_whatsnew(cfg: config.Config) -> int:
    """Print the pull digest: every synced file changed on the team branch since
    this machine's last sync (the manifest horizon), with best-effort who/when/
    why (see mooring.whatsnew). Read-only — pull applies, this only reports."""
    from mooring import sync, whatsnew

    client = _client(cfg)
    report = sync.status(client, cfg)
    digest = whatsnew.pending_digest(client, cfg, report)
    if not digest.entries:
        print("Nothing new — no teammate changes waiting since your last sync.")
        return 0
    width = max(len(e.path) for e in digest.entries)
    state_width = max(len(e.state) for e in digest.entries)
    for e in digest.entries:
        who = ", ".join(e.authors)
        when = (e.date or "")[:10]
        message = e.messages[0] if e.messages else ""
        detail = " · ".join(part for part in (who, when, message) if part)
        line = f"  {e.path:<{width}}  {e.state:<{state_width}}"
        print(f"{line}  {detail}".rstrip())
    if not digest.attributed:
        print("(couldn't read the commit history — showing sync states only)")
    if digest.truncated:
        print("(a long window — GitHub truncated the commit list; attribution may be partial)")
    print(f"{len(digest.entries)} file(s) changed on {cfg.branch} since your last sync.")
    conflicts = sum(1 for e in digest.entries if e.state == sync.FileState.CONFLICT.value)
    if conflicts:
        print(f"{conflicts} conflicted file(s) need resolving (the hub, or mooring pull --theirs).")
    print("Apply the changes with: mooring pull")
    return 0


def cmd_restore(
    cfg: config.Config, rel_path: str, at: str, as_copy: bool, assume_yes: bool
) -> int:
    from mooring import notebook_undo, sync

    client = _client(cfg)
    workspace = cfg.workspace()
    if not as_copy and not assume_yes:
        if not sys.stdin.isatty():
            sys.exit(
                f"Refusing to overwrite {rel_path} without confirmation. "
                "Re-run with --yes (or use --copy)."
            )
        prompt = (
            f"Replace your current {rel_path} with the version at {at[:7]}? "
            "Your current bytes are saved first. [y/N] "
        )
        if input(prompt).strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0

    def snapshot_fn(rel: str, data: bytes) -> None:
        if rel.endswith(".py"):
            notebook_undo.snapshot(workspace, rel, data)

    result = sync.restore_version(
        client, cfg, rel_path, at, as_copy=as_copy, snapshot_fn=snapshot_fn
    )
    telemetry.log_event("restore", copy=int(as_copy), reverted=result.reverted)
    _record_activity(cfg, "restore", result, path=rel_path)
    _print_sync_result(result)
    return 0 if result.reverted else 1


def cmd_trash(cfg: config.Config, args: argparse.Namespace) -> int:
    from mooring import trash

    workspace = cfg.workspace()
    if args.trash_command == "list":
        entries = trash.entries(workspace)
        if not entries:
            print("The trash is empty.")
            return 0
        for e in entries:
            print(f"  {e['ts']}  {e['path']}  ({e['action']})")
            print(f"    restore with: mooring trash restore {e['token']}")
        return 0
    # restore
    try:
        rel = trash.restore(workspace, args.token)
    except KeyError:
        sys.exit(f"Unknown or expired trash entry: {args.token}")
    except trash.RestoreSuperseded as exc:
        sys.exit(
            f"Not restored: {exc} has changed since this copy was saved, so restoring "
            "it would overwrite newer work."
        )
    telemetry.log_event("trash_restore")
    _record_activity(cfg, "trash_restore", path=rel)
    print(f"Restored {rel} from the trash.")
    return 0


def cmd_activity(cfg: config.Config, args: argparse.Namespace) -> int:
    entries = activity.read(cfg.workspace(), limit=args.limit, path=args.path)
    if not entries:
        print("Nothing recorded yet.")
        return 0
    for e in entries:
        detail = e.get("summary") or e.get("path") or ""
        print(f"  {e['ts']}  {e['op']}  {detail}".rstrip())
    return 0


def cmd_rollback(
    cfg: config.Config, rel_path: str, assume_yes: bool, include_conflict: bool
) -> int:
    from mooring import notebook_undo, sync

    client = _client(cfg)  # last-synced bytes come from GitHub — needs login
    workspace = cfg.workspace()
    if not assume_yes:
        if not sys.stdin.isatty():
            sys.exit(
                f"Refusing to discard local changes to {rel_path} without confirmation. "
                "Re-run with --yes."
            )
        prompt = f"Discard your changes to {rel_path} and restore the last synced version? [y/N] "
        if input(prompt).strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 0

    # Snapshot the current notebook bytes before overwriting so the revert is itself
    # undoable (the hub's Undo, or /api/ai/chat/rollback, can restore it). Only .py
    # rides the notebook undo stack — that is the channel its restore reloads.
    def snapshot_fn(rel: str, data: bytes) -> None:
        if rel.endswith(".py"):
            notebook_undo.snapshot(workspace, rel, data)

    result = sync.revert(
        client, cfg, rel_path, include_conflict=include_conflict, snapshot_fn=snapshot_fn
    )
    telemetry.log_event("rollback", reverted=result.reverted, lines=len(result.lines))
    _record_activity(cfg, "rollback", result, path=rel_path)
    _print_sync_result(result)
    return 0


def cmd_repo(app_cfg: config.AppConfig, args: argparse.Namespace) -> int:
    from mooring import config_store

    if args.repo_command == "list":
        if not app_cfg.repos:
            print("No repos registered. Run `mooring repo add <owner>/<repo>`.")
            return 0
        width = max(len(s.alias) for s in app_cfg.repos)
        for s in app_cfg.repos:
            marker = "*" if s.alias == app_cfg.active_alias else " "
            ws = app_cfg.config_for(s.alias).workspace()
            print(f"  {marker} {s.alias:<{width}}  {s.slug} @ {s.branch}  ({ws})")
        return 0
    if args.repo_command == "add":
        owner, _, repo = args.slug.partition("/")
        if not owner or not repo or "/" in repo:
            sys.exit(f"Expected owner/repo (e.g. acme/notebooks), got {args.slug!r}.")
        alias = args.alias or repo
        try:
            config_store.add_repo(
                alias,
                owner,
                repo,
                branch=args.branch,
                workspace=args.workspace,
                make_active=not args.no_use,
                host=args.host,
            )
        except ValueError as exc:
            sys.exit(str(exc))
        telemetry.log_event("repo_add", alias=alias)
        active = " (now active)" if not args.no_use else ""
        print(f"Registered {owner}/{repo} as {alias!r}{active}.")
        return 0
    if args.repo_command == "use":
        try:
            config_store.set_active(args.alias)
        except KeyError:
            sys.exit(_unknown_alias(args.alias, app_cfg))
        telemetry.log_event("repo_switch", alias=args.alias)
        print(f"Active repo is now {args.alias!r}.")
        return 0
    if args.repo_command == "remove":
        if getattr(args, "all_repos", False):
            aliases = list(app_cfg.aliases)
            if not aliases:
                print("No repos registered.")
                return 0
            config_store.remove_all_repos()
            telemetry.log_event("repo_remove", alias="*")
            print(
                f"Removed all {len(aliases)} repo(s): {', '.join(aliases)}. "
                "Workspace folders were kept; delete them manually."
            )
            return 0
        if not args.alias:
            sys.exit("Specify a repo alias to remove, or use --all.")
        try:
            ws = app_cfg.config_for(args.alias).workspace()
            config_store.remove_repo(args.alias)
        except KeyError:
            sys.exit(_unknown_alias(args.alias, app_cfg))
        telemetry.log_event("repo_remove", alias=args.alias)
        print(f"Removed {args.alias!r}. Workspace folder {ws} was kept; delete it manually.")
        return 0
    return 2


def _unknown_alias(alias: str, app_cfg: config.AppConfig) -> str:
    known = ", ".join(app_cfg.aliases) or "(none)"
    return f"Unknown repo alias {alias!r}. Known: {known}"


def cmd_ai_dictionary_check(app_cfg: config.AppConfig, cfg: config.Config) -> int:
    """Parse the workspace's data dictionary and report what mooring understood.

    Runs offline (no Copilot needed) so a team can validate their YAML — and the
    secret scan — before enabling the feature or pushing context to the team.
    """
    from mooring.ai import datadictionary, scan

    workspace = cfg.workspace()
    ctx_dir = app_cfg.ai_context_dir
    index = datadictionary.load_index(workspace, ctx_dir)
    if not app_cfg.ai_context:
        print("Note: [ai] context is OFF — set it true to actually use this in the chat.\n")
    if not index.reports:
        print(
            f"No dictionary files under {ctx_dir}/dictionaries/*.yaml or {ctx_dir}/datadictionary.yaml."
        )
        return 0
    for r in index.reports:
        if r.error:
            print(f"  {r.path}: ERROR — {r.error}")
            continue
        print(f"  {r.path}: detected {r.shape} - {r.n_tables} tables, {r.n_columns} columns")
        if r.dropped_keys:
            print(f"      dropped keys: {', '.join(r.dropped_keys)}")
    if index.tables:
        print("\nSample parsed table:")
        for line in datadictionary.render_table(index.tables[0], max_cols=8).splitlines():
            print(f"  {line}")
    findings = scan.scan_context_secrets(workspace, ctx_dir, index)
    print("")
    if findings:
        print(f"secret scan: {len(findings)} high-confidence finding(s) - fix before sharing:")
        for path, line, kind in findings:
            print(f"  {path}:{line}  {kind}")
        return 1
    print("secret scan: clean (best-effort - not a guarantee; never paste real values)")
    return 0


def cmd_ai_model_check(app_cfg: config.AppConfig, cfg: config.Config) -> int:
    """Show exactly what the copilot would see of each Power BI semantic model.

    The `ai dictionary check` idiom, offline (no Copilot, no network): per model,
    which definition files were read, which tables/measures were kept, what the
    allowlist excluded (partition M bodies skipped uncaptured; roles/translations
    never opened), plus the egress scrubber's value-free findings over everything
    the model tools could render. Exit 1 on scrubber findings, so it doubles as a
    pre-share lint and the TMDL-drift detector.
    """
    from mooring import pbip_model
    from mooring.ai import egress

    workspace = cfg.workspace()
    refs = pbip_model.find_models(workspace, cfg.folders)
    if not app_cfg.ai_semantic_model:
        print("Note: [ai] semantic_model is OFF - the copilot will not see these models.\n")
    if not refs:
        print(
            "No Power BI semantic models (<name>.SemanticModel/definition/) "
            "under the synced folders."
        )
        return 0
    exit_code = 0
    for ref in refs:
        model = pbip_model.extract_model(ref.path, key=ref.key, name=ref.name)
        opted_out = workspace_config.is_semantic_model_disabled(workspace, ref.key)
        print(f"{ref.key}{pbip_model.MODEL_DIR_SUFFIX}:")
        if opted_out:
            print("  AI access: OFF for this model (mooring.toml [ai] disabled_semantic_models)")
        for rel in model.files_read:
            print(f"  read {rel}")
        for note in model.notes:
            print(f"  note: {note}")
        print(f"  kept: {len(model.tables)} tables, {model.n_measures} measures, "
              f"{len(model.relationships)} relationships")
        ex = model.excluded
        excluded_bits = []
        if ex.partitions:
            excluded_bits.append(f"{ex.partitions} partition/source block(s) (never captured)")
        if ex.roles_files:
            excluded_bits.append(f"{ex.roles_files} roles file(s) (never opened)")
        if ex.culture_files:
            excluded_bits.append(f"{ex.culture_files} translation file(s) (never opened)")
        if excluded_bits:
            print(f"  excluded: {', '.join(excluded_bits)}")
        if ex.dropped:
            dropped = ", ".join(f"{k} x{n}" for k, n in ex.dropped)
            print(f"  dropped constructs/properties: {dropped}")
        # The scrubber pre-flight: everything the model tools could render, through
        # the same egress.scrub_text the tools apply. Findings are value-free.
        rendered = [pbip_model.render_summary(model)]
        rendered += [pbip_model.render_table(model, t) for t in model.tables]
        _, findings = egress.scrub_text("\n".join(rendered))
        if findings:
            exit_code = 1
            print(f"  scrub: {len(findings)} finding(s) - these lines would be withheld:")
            for f in findings:
                print(f"    line {f.line}  {f.kind}")
        else:
            print("  scrub: clean (best-effort - not a guarantee; DAX is authored code)")
        print("")
    return exit_code


_PII_FOOTER = (
    "\n(best-effort - detects well-formed cards, IBANs, NHS numbers, emails, and UK NINOs;\n"
    " never sort codes, account numbers, SSNs, or phones. Names need detect_names + the\n"
    " mooring[pii] extra. Put `# mooring: pii-ok` on a line to retire a reviewed false\n"
    " positive. Never paste real values.)"
)


def cmd_ai_pii_check(
    app_cfg: config.AppConfig, cfg: config.Config, args: argparse.Namespace
) -> int:
    """Scan context/ and notebook sources for PII, offline.

    Mirrors ``ai dictionary check``: no Copilot, no network, value-free output —
    a team can lint their files before enabling the guard or sharing context. When
    ``detect_names`` is on and the ``mooring[pii]`` extra is installed, the local
    NER name pass runs too; otherwise it scans structured PII only.
    """
    from mooring.ai import datadictionary, ner, scan

    workspace = cfg.workspace()
    ctx_dir = app_cfg.ai_context_dir
    index = datadictionary.load_index(workspace, ctx_dir)
    backend = ner.resolve_backend(app_cfg.ai_pii_name_backend)
    model = ner.model_for(
        backend,
        app_cfg.ai_pii_name_model,
        app_cfg.ai_pii_name_revision,
        app_cfg.ai_pii_name_variant,
    )
    # Only run NER when the model is already present — a lint must not trigger a
    # surprise download. Otherwise fall back to structured-only with a note.
    names = app_cfg.ai_pii_names and ner.available(backend) and ner.is_ready(model, backend)
    if app_cfg.ai_pii_names and not ner.available(backend):
        extra = "pii-spacy" if backend == "spacy" else "pii"
        print(
            f"Note: detect_names is ON but the '{extra}' extra isn't installed - scanning\n"
            f"      structured PII only. Install it: pip install mooring[{extra}]\n"
        )
    elif app_cfg.ai_pii_names and not names:
        hint = (
            "the spaCy model isn't present (install mooring[pii-spacy] or bundle it)"
            if backend == "spacy"
            else "the model isn't downloaded yet (fetch it: mooring ai pii model)"
        )
        print(f"Note: detect_names is ON but {hint} - scanning structured PII only.\n")
    findings = scan.scan_pii_targets(
        workspace,
        ctx_dir,
        cfg.folders,
        index,
        getattr(args, "notebook", None),
        names=names,
        labels=app_cfg.ai_pii_name_labels,
        threshold=app_cfg.ai_pii_name_threshold,
        model=model,
        backend=backend,
    )
    if not app_cfg.ai_pii:
        print("Note: [ai.pii] enabled is OFF - set it true to actually enforce this in the chat.\n")
    if findings:
        print(f"pii scan: {len(findings)} finding(s) - review before sharing or using the copilot:")
        for path, line, kind in findings:
            print(f"  {path}:{line}  {kind}")
        print(_PII_FOOTER)
        return 1
    print("pii scan: clean (best-effort — not a guarantee; never paste real values)")
    return 0


def cmd_ai_traceback_check(
    app_cfg: config.AppConfig, cfg: config.Config, args: argparse.Namespace
) -> int:
    """Show exactly how the traceback guard would rewrite a pasted traceback, offline.

    Mirrors ``ai pii check``: no Copilot, no network — a security reviewer can see
    what survives sanitising before trusting the chat guard. Reads FILE, or stdin
    when no file is given. The chat additionally rescues exception messages whose
    quoted tokens are already in the session's context (live schema, notebook
    source); this offline preview has no session, so it redacts more, never less.
    """
    del app_cfg  # symmetric signature with the other `ai` commands; not needed here
    from mooring.ai import egress

    if getattr(args, "file", None):
        try:
            text = Path(args.file).read_text("utf-8")
        except OSError as exc:
            print(f"Could not read {args.file}: {exc}")
            return 1
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("Nothing to check — paste a traceback on stdin or give a FILE.")
        return 1
    result = egress.sanitize_traceback(text, workspace=cfg.workspace())
    if not result.detected:
        print("No traceback detected — the text would be sent unchanged")
        print("(after the usual outbound PII scan, when that guard is enabled).")
        return 0
    print("Sanitised rewrite (what the assistant would receive):")
    print()
    print(result.text)
    print()
    if result.findings:
        print(f"redactions: {len(result.findings)}")
        for f in result.findings:
            print(f"  line {f.line}  {f.kind}")
    else:
        print("redactions: none (everything was provably value-free)")
    print(
        "\n(the raw paste is never stored or sent — the chat holds this rewrite for a"
        "\n 'Send sanitised' confirm; in the chat, exception messages also survive when"
        "\n every quoted token is already in the session's schema/notebook context)"
    )
    return 0


def cmd_ai_pii_model(
    app_cfg: config.AppConfig, cfg: config.Config, args: argparse.Namespace
) -> int:
    """Pre-fetch (GLiNER) or verify (spaCy) the local NER name-detection model.

    GLiNER downloads its weights from Hugging Face on first use; running this once
    means the first flagged chat prompt isn't blocked on a surprise download. The
    spaCy backend has nothing to download (the model ships via the pii-spacy extra
    or is bundled), so this just confirms it loads.
    """
    from mooring.ai import ner

    configured = app_cfg.ai_pii_name_backend
    backend = ner.resolve_backend(configured)
    if (configured or "").strip().lower() not in ("gliner", "spacy"):
        print(f"name_backend = {configured!r} resolved to the {backend} backend.\n")
    if backend == "spacy":
        return _cmd_ai_pii_model_spacy(app_cfg, ner.model_for("spacy", app_cfg.ai_pii_name_model))

    if not ner.available("gliner"):
        print("The 'pii' extra is not installed. Install it: pip install mooring[pii]")
        return 1
    ref = ner.ModelRef(
        app_cfg.ai_pii_name_model, app_cfg.ai_pii_name_revision, app_cfg.ai_pii_name_variant
    )
    label = app_cfg.ai_pii_name_model + (f" (variant {ref.variant})" if ref.variant else "")
    if ner.is_cached(ref):
        print(f"NER model {label} is already downloaded and ready.")
        if not app_cfg.ai_pii_names:
            print("Note: [ai.pii] detect_names is OFF - set it (and enabled) true to use it.")
        return 0
    print(f"Downloading NER model {label} from Hugging Face (safetensors only).")
    print("This is a one-time download and resumes if interrupted;")
    print("it caches under your home directory and won't download again on later runs.\n")
    try:
        ner.download_model(ref, on_progress=_print_download_progress)
        ner.load_model(ref)
    except ner.NerUnavailable as exc:
        print(f"\nFailed: {exc}")
        print("Tip: an HF_TOKEN raises the anonymous rate limit; a smaller model id in")
        print("[ai.pii] name_model downloads faster. Re-run to resume.")
        return 1
    print("\nOK - model is cached and ready for offline name detection.")
    if not app_cfg.ai_pii_names:
        print(
            "Note: [ai.pii] detect_names is OFF - set it (and enabled) true to use it in the chat."
        )
    return 0


def _cmd_ai_pii_model_spacy(app_cfg: config.AppConfig, model: str) -> int:
    """Verify the offline spaCy model loads (nothing to download). ``model`` is the
    spaCy model name/path ("" = the bundled companion), already shaped for spaCy."""
    from mooring.ai import ner_spacy

    if not ner_spacy.available():
        print("The 'pii-spacy' extra is not installed. Install it: pip install mooring[pii-spacy]")
        return 1
    if ner_spacy.is_ready(model):
        print(f"spaCy model ({model or 'bundled mooring-spacy-en-md'}) is present and loads -")
        print("ready for offline name detection (no Hugging Face / internet needed).")
        if not app_cfg.ai_pii_names:
            print("Note: [ai.pii] detect_names is OFF - set it (and enabled) true to use it.")
        return 0
    print("The spaCy model isn't available. Either install mooring[pii-spacy] (which bundles")
    print("en_core_web_md from PyPI), or set [ai.pii] name_model to a model name / folder you")
    print("have sideloaded. No Hugging Face or internet is needed either way.")
    return 1


def cmd_ai_pii_doctor(app_cfg: config.AppConfig) -> int:
    """End-to-end check of the PII guard config: which name backend will actually
    run, whether its extra + model are present, and exactly what to flip to enforce.

    Read-only — it never edits config or downloads anything; it prints the
    `mooring config set ...` / install commands for anything that's off. Exit 0 when
    the guard is ready (or name detection is intentionally off); 1 when name
    detection is requested but its backend isn't ready (so it would silently fall
    back to structured-only).
    """
    from mooring.ai import ner, ner_spacy

    enabled = app_cfg.ai_pii
    names = app_cfg.ai_pii_names
    configured = app_cfg.ai_pii_name_backend
    backend = ner.resolve_backend(configured)
    pinned = (configured or "").strip().lower() in ("gliner", "spacy")

    print("PII guard (ai.pii):\n")
    print(f"  guard enabled:     {'on' if enabled else 'OFF'}")
    print(
        f"  prompt on a hit:   {'block + confirm' if app_cfg.ai_pii_block_prompt else 'warn-only'}"
    )
    print("  structured scan:   always on  (cards, IBANs, NHS numbers, emails, UK NINOs)")
    print(f"  name detection:    {'on' if names else 'off'}")
    print(
        f"  name backend:      {backend}  (pinned)"
        if pinned
        else f"  name backend:      {configured} -> {backend}"
    )

    # Backend/model readiness. `ready` only matters when name detection is on.
    ready = True
    todo: list[str] = []
    if backend == "spacy":
        if not ner_spacy.available():
            ready = False
            print("  spaCy model:       the 'pii-spacy' extra is not installed")
            todo.append(
                "pip install mooring[pii-spacy]   # spaCy + bundled model, offline from PyPI"
            )
        else:
            mdl = ner.model_for("spacy", app_cfg.ai_pii_name_model)
            if ner_spacy.is_ready(mdl):
                print(
                    f"  spaCy model:       {mdl or 'bundled mooring-spacy-en-md'} present, loads OK (offline)"
                )
            else:
                ready = False
                print(f"  spaCy model:       not found ({mdl or 'bundled companion missing'})")
                todo.append(
                    "pip install mooring[pii-spacy]   # or set ai.pii.name_model to a model you have"
                )
    else:  # gliner
        if not ner.available("gliner"):
            ready = False
            print("  GLiNER model:      the 'pii' extra is not installed")
            todo.append(
                "pip install mooring[pii]   # GLiNER (downloads its model from Hugging Face)"
            )
        else:
            ref = ner.model_for(
                "gliner",
                app_cfg.ai_pii_name_model,
                app_cfg.ai_pii_name_revision,
                app_cfg.ai_pii_name_variant,
            )
            if ner.is_cached(ref):
                print(f"  GLiNER model:      {app_cfg.ai_pii_name_model} cached, ready (offline)")
            else:
                ready = False
                print(f"  GLiNER model:      {app_cfg.ai_pii_name_model} not downloaded yet")
                todo.append("mooring ai pii model   # one-time download from Hugging Face")

    print()
    if names and not ready:
        print(f"-> name detection is ON but the {backend} backend isn't ready: names won't")
        print("   be scanned (the structured scan still runs when the guard is on).")
        print("   Install / prepare it:")
        for step in todo:
            print(f"     {step}")
    elif enabled and names:
        print(f"-> ready: the guard enforces in the chat, names via the {backend} backend.")
    elif enabled:
        print("-> ready: the structured scan enforces in the chat (name detection is off).")
    else:
        print("-> the guard is OFF, so nothing is enforced in the chat yet.")

    flips = []
    if not enabled:
        flips.append("mooring config set ai.pii.enabled true       # turn the guard on")
    if not names:
        flips.append(
            "mooring config set ai.pii.detect_names true  # optional: also catch person/org names"
        )
    if flips:
        print("\nConfig:")
        for flip in flips:
            print(f"  {flip}")

    return 0 if (not names or ready) else 1


def _print_download_progress(done: int, total: int) -> None:
    pct = int(done * 100 / total) if total else 0
    mb = 1024 * 1024
    sys.stdout.write(f"\r  downloading… {pct:3d}%  ({done // mb} / {total // mb} MB)")
    sys.stdout.flush()


def cmd_ai(app_cfg: config.AppConfig, cfg: config.Config, args: argparse.Namespace) -> int:
    """Manage the AI copilot: Copilot sign-in (login / status), dictionary check,
    the offline PII pre-flight scan (pii check), and the offline traceback-rewrite
    preview (traceback check).

    Code generation lives in the interactive chat (hub "AI" button), not the CLI.
    """
    from mooring.ai import AIError, get_provider

    if args.ai_command == "dictionary":
        if args.ai_dict_command == "check":
            return cmd_ai_dictionary_check(app_cfg, cfg)
        return 2

    if args.ai_command == "model":
        if args.ai_model_command == "check":
            return cmd_ai_model_check(app_cfg, cfg)
        return 2

    if args.ai_command == "pii":
        if args.ai_pii_command == "check":
            return cmd_ai_pii_check(app_cfg, cfg, args)
        if args.ai_pii_command == "model":
            return cmd_ai_pii_model(app_cfg, cfg, args)
        if args.ai_pii_command == "doctor":
            return cmd_ai_pii_doctor(app_cfg)
        return 2

    if args.ai_command == "traceback":
        if args.ai_traceback_command == "check":
            return cmd_ai_traceback_check(app_cfg, cfg, args)
        return 2

    try:
        provider = get_provider(app_cfg)
    except AIError as exc:
        sys.exit(str(exc))

    if args.ai_command == "login":
        if not provider.available():
            sys.exit("Copilot isn't available. Install the extra: pip install mooring[copilot]")
        print("Opening Copilot sign-in — a browser window will open to authorize…")
        code = provider.login_interactive(host=args.host)
        print("Copilot sign-in complete." if code == 0 else "Copilot sign-in did not complete.")
        return code

    if args.ai_command == "status":
        st = provider.status(force=True)
        state = (
            "connected"
            if st.connected
            else ("unavailable" if not st.available else "not connected")
        )
        who = f" as {st.account}" if st.account else ""
        print(f"AI provider : {app_cfg.ai_provider}")
        print(f"  status    : {state}{who}")
        if st.detail:
            print(f"  detail    : {st.detail}")
        return 0 if st.connected else 1
    return 2


def _coerce_config_value(values: list[str]):
    """Type a ``config set`` value. Several tokens become a string list; a single
    token is parsed as a TOML value (``true``/``false`` -> bool, ``5`` -> int,
    ``0.7`` -> float, ``["a","b"]`` -> list) and falls back to a bare string when
    that doesn't parse — so paths/ids like ``urchade/gliner_multi_pii-v1`` stay
    strings. Quote a value (``'"123"'``) to force a string that looks numeric."""
    import tomllib

    if len(values) > 1:
        return list(values)
    raw = values[0]
    try:
        return tomllib.loads(f"_v = {raw}")["_v"]
    except Exception:  # noqa: BLE001  # not a TOML literal -> treat as a bare string
        return raw


def _format_config_value(value) -> str:
    """Render a config value the way it appears in TOML (true, 0.7, ["a", "b"])."""
    import tomli_w

    if isinstance(value, dict):
        return tomli_w.dumps(value).rstrip()
    return tomli_w.dumps({"v": value}).split("=", 1)[1].strip()


def cmd_config(args: argparse.Namespace) -> int:
    """View and edit the user config.toml via dotted keys (e.g. ai.pii.enabled).

    Writes ONLY the user file, preserving the rest; ``get``/``list`` show the
    effective values (default merged with the file), not env-var overrides.
    """
    import tomli_w

    from mooring import config_store, paths

    sub = args.config_command
    if sub == "path":
        print(paths.user_config_file())
        return 0
    if sub == "list":
        print(tomli_w.dumps(config.merged_data()).rstrip())
        return 0
    if sub == "get":
        try:
            value = config_store.get_value(args.key)
        except ValueError as exc:
            sys.exit(str(exc))
        except KeyError:
            sys.exit(f"No such setting: {args.key}")
        print(_format_config_value(value))
        return 0
    if sub == "set":
        value = _coerce_config_value(args.value)
        try:
            config_store.set_value(args.key, value)
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"Set {args.key} = {_format_config_value(value)}")
        print(f"  in {paths.user_config_file()}")
        return 0
    if sub == "unset":
        try:
            removed = config_store.unset_value(args.key)
        except ValueError as exc:
            sys.exit(str(exc))
        print(
            f"Unset {args.key} (reverted to default)."
            if removed
            else f"{args.key} was not set in your config; nothing to do."
        )
        return 0
    return 2


def _dispatch(
    parser: argparse.ArgumentParser,
    command: str,
    app_cfg: config.AppConfig,
    cfg: config.Config,
    args: argparse.Namespace,
) -> int:
    if command == "version":
        print(f"mooring {__version__}")
        return 0
    if command == "repo":
        return cmd_repo(app_cfg, args)
    if command == "config":
        return cmd_config(args)
    if command == "ai":
        return cmd_ai(app_cfg, cfg, args)
    if command == "selftest":
        return cmd_selftest(app_cfg, cfg)
    if command == "doctor":
        return cmd_doctor(app_cfg, cfg, args.report)
    if command == "hub":
        from mooring.hub.server import run_hub

        no_browser = getattr(args, "no_browser", False)
        port = getattr(args, "port", None)
        return run_hub(app_cfg, open_browser=not no_browser, port=port)
    if command == "login":
        return cmd_login(cfg, getattr(args, "host", None))
    if command == "logout":
        return cmd_logout(cfg)
    if command == "whoami":
        return cmd_whoami(cfg)
    if command == "status":
        return cmd_status(cfg)
    if command == "pull":
        return cmd_pull(cfg, args.theirs, args.keep_both)
    if command == "push":
        return cmd_push(cfg, args.paths, args.message, args.acknowledge_findings)
    if command == "propose":
        return cmd_propose(cfg, args.paths, args.message, args.acknowledge_findings)
    if command == "scan":
        return cmd_scan(cfg)
    if command == "recall":
        return cmd_recall(cfg, args.yes)
    if command == "adopt":
        return cmd_adopt(cfg, args.folders, args.all_folders)
    if command == "open":
        return cmd_open(cfg, args.path)
    if command == "new":
        return cmd_new(cfg, args.name)
    if command == "duplicate":
        return cmd_duplicate(cfg, args.path)
    if command == "init":
        return cmd_init(cfg)
    if command == "deps":
        return cmd_deps(cfg, args)
    if command == "shadow":
        return cmd_shadow(cfg, args)
    if command == "build-requirements":
        return cmd_build_requirements(cfg, args.output)
    if command == "delete":
        return cmd_delete(cfg, args.path, args.yes)
    if command == "rollback":
        return cmd_rollback(cfg, args.path, args.yes, args.conflicts)
    if command == "trash":
        return cmd_trash(cfg, args)
    if command == "activity":
        return cmd_activity(cfg, args)
    if command == "history":
        return cmd_history(cfg, args.path, args.page)
    if command == "whatsnew":
        return cmd_whatsnew(cfg)
    if command == "restore":
        return cmd_restore(cfg, args.path, args.at, args.copy, args.yes)
    parser.error(f"unknown command {command!r}")
    return 2


def main(argv: list[str] | None = None) -> int:
    _inject_truststore()
    _ensure_child_pythonpath()
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "hub"
    try:
        app_cfg = config.load_app_config()
    except ValueError as exc:  # e.g. a malformed [github] host
        sys.exit(str(exc))
    try:
        cfg = app_cfg.config_for(getattr(args, "repo", None))
    except KeyError:
        sys.exit(_unknown_alias(args.repo, app_cfg))
    # Fold the repo's synced sub-folders (mooring.toml [sync] folders) into the scope,
    # so notebooks under a uv-workspace package folder list and sync like the hub's do.
    from dataclasses import replace

    folders = workspace_config.merge_extra_folders(cfg.folders, cfg.workspace())
    if folders != cfg.folders:
        cfg = replace(cfg, folders=folders)

    telemetry.configure(
        app_cfg.log_endpoint,
        identity=telemetry.base_identity(),
        level=app_cfg.log_level,
    )
    telemetry.log_event("app_start", command=command)

    from mooring.github import Unreachable

    try:
        return _dispatch(parser, command, app_cfg, cfg, args)
    except SystemExit:
        raise  # user-facing errors (sys.exit / argparse) are not app failures
    except Unreachable as exc:
        # An outage is not an app failure worth a traceback: one classified line.
        telemetry.log_error(exc=exc, command=command)
        sys.exit(str(exc))
    except BaseException as exc:  # noqa: BLE001  # record genuine failures, then re-raise
        telemetry.log_error(exc=exc, command=command)
        raise


if __name__ == "__main__":
    sys.exit(main())
