"""Command-line entry point for mooring."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from mooring import __version__, config, paths, pyproject_env, telemetry

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
    except Exception as exc:  # noqa: BLE001 - never let TLS setup brick the app
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
    repo_add.add_argument(
        "--no-use", action="store_true", help="register without switching to it"
    )
    repo_use = repo_sub.add_parser("use", help="switch the active repo")
    repo_use.add_argument("alias")
    repo_rm = repo_sub.add_parser("remove", help="forget a repo (local files are kept)")
    repo_rm.add_argument("alias", nargs="?", default=None, help="alias to remove (omit when using --all)")
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

    propose = sub.add_parser(
        "propose", help="upload changes to a review branch (open a pull request on GitHub)"
    )
    propose.add_argument(
        "paths", nargs="*", help="specific files to propose (default: all changes)"
    )
    propose.add_argument("-m", "--message", default=None, help="commit message")

    open_cmd = sub.add_parser("open", help="open a notebook in the marimo editor")
    open_cmd.add_argument("path", help="workspace-relative notebook path")

    new = sub.add_parser("new", help="create a new notebook and open it")
    new.add_argument("name", help="notebook name (e.g. sales-analysis)")

    delete_cmd = sub.add_parser(
        "delete", help="delete a notebook from the workspace (push afterwards to remove it remotely)"
    )
    delete_cmd.add_argument(
        "path", help="workspace-relative notebook path (a .py file or a .pbip project)"
    )
    delete_cmd.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )

    init_cmd = sub.add_parser(
        "init",
        help="create the repo's pyproject.toml (its notebook dependencies) and lock it",
    )

    deps = sub.add_parser("deps", help="manage the repo's notebook dependencies")
    deps_sub = deps.add_subparsers(dest="deps_command", required=True)
    deps_add = deps_sub.add_parser("add", help="add packages to the repo and re-lock")
    deps_add.add_argument(
        "packages", nargs="+", help="packages to add (e.g. polars 'scipy>=1.11')"
    )
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

    for cmd in (status, pull, push, propose, open_cmd, new, delete_cmd, init_cmd, deps, build_reqs):
        cmd.add_argument(
            "--repo", default=None, metavar="ALIAS", help="act on this repo instead of the active one"
        )

    ai = sub.add_parser("ai", help="AI copilot: sign in to Copilot and check status")
    ai_sub = ai.add_subparsers(dest="ai_command", required=True)
    ai_sub.add_parser("status", help="show the AI provider's sign-in status")
    ai_login = ai_sub.add_parser("login", help="sign in to Copilot (OAuth device flow)")
    ai_login.add_argument(
        "--host", default=None, help="GitHub host URL for Copilot (GHE data residency)"
    )

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


def cmd_selftest(app_cfg: config.AppConfig, cfg: config.Config) -> int:
    import importlib.metadata

    print(f"mooring {__version__}  (python {sys.version.split()[0]}, {sys.executable})")
    failures = []
    for name in SELFTEST_PACKAGES:
        try:
            importlib.import_module(name)
            version = importlib.metadata.version(name)
            print(f"  ok  {name} {version}")
        except Exception as exc:  # noqa: BLE001 - report and continue
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
    from mooring.github import GitHubClient

    if not cfg.is_configured:
        sys.exit(
            "No team repo configured. Set [github] owner/repo/client_id in "
            f"{paths.user_config_file()} (or run the hub for guided setup)."
        )
    return GitHubClient(_require_token(cfg), cfg.owner, cfg.repo, host=cfg.host)


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
            "No OAuth client_id configured. Set [github] client_id in "
            f"{paths.user_config_file()}."
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

    report = sync.status(_client(cfg), cfg)
    if not report.files:
        print("Workspace empty and no remote files. Try `mooring new <name>`.")
        return 0
    width = max(len(f.path) for f in report.files)
    for f in report.files:
        print(f"  {f.path:<{width}}  {f.state.value}")
    print(report.summary())
    if report.review_branch:
        print(f"proposal open on {report.review_branch}")
    return 0


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
    for line in result.lines:
        print(f"  {line}")
    print(result.summary())
    return 0 if not result.skipped_conflicts else 1


def cmd_push(cfg: config.Config, only_paths: list[str], message: str | None) -> int:
    from mooring import sync

    result = sync.push(_client(cfg), cfg, paths=only_paths or None, message=message)
    telemetry.log_event(
        "push",
        pushed=result.pushed,
        conflicts=len(result.blocked_conflicts),
        lines=len(result.lines),
    )
    for line in result.lines:
        print(f"  {line}")
    print(result.summary())
    return 0 if not result.blocked_conflicts else 1


def cmd_propose(cfg: config.Config, only_paths: list[str], message: str | None) -> int:
    from mooring import sync

    result = sync.propose(_client(cfg), cfg, paths=only_paths or None, message=message)
    telemetry.log_event(
        "propose",
        proposed=result.proposed,
        conflicts=len(result.blocked_conflicts),
        review_branch=bool(result.review_branch),
    )
    for line in result.lines:
        print(f"  {line}")
    print(result.summary())
    return 0 if not result.blocked_conflicts else 1


def cmd_open(cfg: config.Config, rel_path: str) -> int:
    import webbrowser

    from mooring.editor import EditorServer

    workspace = cfg.workspace()
    target = workspace / rel_path
    if not target.is_file():
        sys.exit(f"No such notebook: {target}")
    if rel_path.endswith(".pbip"):
        from mooring import pbip

        try:
            pbip.launch(target)
        except pbip.PbipLaunchError as exc:
            sys.exit(str(exc))
        telemetry.log_event("open", kind="pbip")
        print(f"Opened {rel_path} in Power BI Desktop.")
        return 0
    server = EditorServer(workspace)
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


def cmd_new(cfg: config.Config, name: str) -> int:
    from mooring import notebook_template

    workspace = cfg.workspace()
    if pyproject_env.scaffold(workspace):
        print(f"Created {pyproject_env.PYPROJECT_NAME} for this repo's notebook dependencies.")
    rel_path = notebook_template.create(workspace, name)
    telemetry.log_event("new")
    print(f"Created {rel_path}")
    return cmd_open(cfg, rel_path)


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
    try:
        removed = deletion.delete(workspace, rel_path, cfg.exclude, cfg.folders)
    except FileNotFoundError:  # vanished between the prompt and the delete
        sys.exit(f"No such notebook: {workspace / rel_path}")
    telemetry.log_event("delete", count=len(removed))
    for r in removed:
        print(f"  deleted {r}")
    print(
        f"Deleted {rel_path} locally. Run `mooring push` (or `propose`) to remove it "
        "from the team repo."
    )
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
                alias, owner, repo,
                branch=args.branch, workspace=args.workspace,
                make_active=not args.no_use, host=args.host,
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


def cmd_ai(app_cfg: config.AppConfig, args: argparse.Namespace) -> int:
    """Manage the AI copilot's Copilot sign-in (login / status).

    Code generation now lives in the interactive chat (hub "AI" button), not the
    CLI; this command only handles authentication and readiness.
    """
    from mooring.ai import AIError, get_provider

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
            "connected" if st.connected else ("unavailable" if not st.available else "not connected")
        )
        who = f" as {st.account}" if st.account else ""
        print(f"AI provider : {app_cfg.ai_provider}")
        print(f"  status    : {state}{who}")
        if st.detail:
            print(f"  detail    : {st.detail}")
        return 0 if st.connected else 1
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
    if command == "ai":
        return cmd_ai(app_cfg, args)
    if command == "selftest":
        return cmd_selftest(app_cfg, cfg)
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
        return cmd_push(cfg, args.paths, args.message)
    if command == "propose":
        return cmd_propose(cfg, args.paths, args.message)
    if command == "open":
        return cmd_open(cfg, args.path)
    if command == "new":
        return cmd_new(cfg, args.name)
    if command == "init":
        return cmd_init(cfg)
    if command == "deps":
        return cmd_deps(cfg, args)
    if command == "build-requirements":
        return cmd_build_requirements(cfg, args.output)
    if command == "delete":
        return cmd_delete(cfg, args.path, args.yes)
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

    telemetry.configure(
        app_cfg.log_endpoint,
        identity=telemetry.base_identity(),
        level=app_cfg.log_level,
    )
    telemetry.log_event("app_start", command=command)

    try:
        return _dispatch(parser, command, app_cfg, cfg, args)
    except SystemExit:
        raise  # user-facing errors (sys.exit / argparse) are not app failures
    except BaseException as exc:  # noqa: BLE001 - record genuine failures, then re-raise
        telemetry.log_error(exc=exc, command=command)
        raise


if __name__ == "__main__":
    sys.exit(main())
