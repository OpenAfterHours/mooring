"""Command-line entry point for mooring."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

from mooring import __version__, config, paths

SELFTEST_PACKAGES = (
    "marimo",
    "polars",
    "altair",
    "plotly",
    "openpyxl",
    "fastexcel",
    "requests",
    "keyring",
    "starlette",
    "uvicorn",
    "platformdirs",
)


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

    sub.add_parser("login", help="log in to GitHub via device flow")
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
        "--no-use", action="store_true", help="register without switching to it"
    )
    repo_use = repo_sub.add_parser("use", help="switch the active repo")
    repo_use.add_argument("alias")
    repo_rm = repo_sub.add_parser("remove", help="forget a repo (local files are kept)")
    repo_rm.add_argument("alias")

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

    open_cmd = sub.add_parser("open", help="open a notebook in the marimo editor")
    open_cmd.add_argument("path", help="workspace-relative notebook path")

    new = sub.add_parser("new", help="create a new notebook and open it")
    new.add_argument("name", help="notebook name (e.g. sales-analysis)")

    for cmd in (status, pull, push, open_cmd, new):
        cmd.add_argument(
            "--repo", default=None, metavar="ALIAS", help="act on this repo instead of the active one"
        )

    sub.add_parser("selftest", help="verify the bundled environment")
    sub.add_parser("version", help="print the version")
    return parser


def _print_paths(cfg: config.Config) -> None:
    print(f"  config file : {paths.user_config_file()}")
    print(f"  workspace   : {cfg.workspace()}")
    print(f"  logs        : {paths.user_log_dir()}")
    hint = legacy_workspace_hint(cfg)
    if hint:
        print(f"  note        : {hint}")


def legacy_workspace_hint(cfg: config.Config) -> str:
    """Warn when a pre-multi-repo workspace exists but the new default doesn't."""
    if not cfg.repo or cfg.workspace_path:
        return ""
    old = paths.legacy_workspace(cfg.repo)
    new = cfg.workspace()
    if old != new and (old / ".mooring").is_dir() and not (new / ".mooring").is_dir():
        return (
            f"Found an old workspace at {old} — move the folder to {new} "
            "(or set its 'workspace' in the config) to keep your sync history."
        )
    return ""


def cmd_selftest(cfg: config.Config) -> int:
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
    if cfg.is_configured:
        print(f"  team repo   : {cfg.repo_slug} (branch {cfg.branch})")
    else:
        print("  team repo   : not configured")
    if failures:
        print(f"selftest FAILED: {', '.join(failures)}")
        return 1
    print("selftest OK")
    return 0


def _require_token() -> str:
    from mooring import auth

    token = auth.get_token()
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
    return GitHubClient(_require_token(), cfg.owner, cfg.repo)


def cmd_login(cfg: config.Config) -> int:
    from mooring import auth

    if not cfg.client_id:
        sys.exit(
            "No OAuth client_id configured. Set [github] client_id in "
            f"{paths.user_config_file()}."
        )
    device = auth.start_device_flow(cfg.client_id)
    print(f"Open {device.verification_uri} and enter code: {device.user_code}")
    print("Waiting for authorization...")
    token = auth.poll_for_token(cfg.client_id, device)
    auth.save_token(token)
    from mooring.github import GitHubClient

    user = GitHubClient(token, cfg.owner, cfg.repo).get_user()
    print(f"Logged in as {user['login']}.")
    return 0


def cmd_logout() -> int:
    from mooring import auth

    auth.delete_token()
    print("Logged out.")
    return 0


def cmd_whoami(cfg: config.Config) -> int:
    from mooring.github import GitHubClient

    user = GitHubClient(_require_token(), cfg.owner, cfg.repo).get_user()
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
    for line in result.lines:
        print(f"  {line}")
    print(result.summary())
    return 0 if not result.skipped_conflicts else 1


def cmd_push(cfg: config.Config, only_paths: list[str], message: str | None) -> int:
    from mooring import sync

    result = sync.push(_client(cfg), cfg, paths=only_paths or None, message=message)
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
        print(f"Opened {rel_path} in Power BI Desktop.")
        return 0
    server = EditorServer(workspace)
    server.ensure_started()
    url = server.url_for(rel_path)
    print(f"Editor running at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def cmd_new(cfg: config.Config, name: str) -> int:
    from mooring import notebook_template

    workspace = cfg.workspace()
    rel_path = notebook_template.create(workspace, name)
    print(f"Created {rel_path}")
    return cmd_open(cfg, rel_path)


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
                make_active=not args.no_use,
            )
        except ValueError as exc:
            sys.exit(str(exc))
        active = " (now active)" if not args.no_use else ""
        print(f"Registered {owner}/{repo} as {alias!r}{active}.")
        return 0
    if args.repo_command == "use":
        try:
            config_store.set_active(args.alias)
        except KeyError:
            sys.exit(_unknown_alias(args.alias, app_cfg))
        print(f"Active repo is now {args.alias!r}.")
        return 0
    if args.repo_command == "remove":
        try:
            ws = app_cfg.config_for(args.alias).workspace()
            config_store.remove_repo(args.alias)
        except KeyError:
            sys.exit(_unknown_alias(args.alias, app_cfg))
        print(f"Removed {args.alias!r}. Workspace folder {ws} was kept; delete it manually.")
        return 0
    return 2


def _unknown_alias(alias: str, app_cfg: config.AppConfig) -> str:
    known = ", ".join(app_cfg.aliases) or "(none)"
    return f"Unknown repo alias {alias!r}. Known: {known}"


def main(argv: list[str] | None = None) -> int:
    _ensure_child_pythonpath()
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "hub"
    app_cfg = config.load_app_config()
    try:
        cfg = app_cfg.config_for(getattr(args, "repo", None))
    except KeyError:
        sys.exit(_unknown_alias(args.repo, app_cfg))

    if command == "version":
        print(f"mooring {__version__}")
        return 0
    if command == "repo":
        return cmd_repo(app_cfg, args)
    if command == "selftest":
        return cmd_selftest(cfg)
    if command == "hub":
        from mooring.hub.server import run_hub

        no_browser = getattr(args, "no_browser", False)
        port = getattr(args, "port", None)
        return run_hub(app_cfg, open_browser=not no_browser, port=port)
    if command == "login":
        return cmd_login(cfg)
    if command == "logout":
        return cmd_logout()
    if command == "whoami":
        return cmd_whoami(cfg)
    if command == "status":
        return cmd_status(cfg)
    if command == "pull":
        return cmd_pull(cfg, args.theirs, args.keep_both)
    if command == "push":
        return cmd_push(cfg, args.paths, args.message)
    if command == "open":
        return cmd_open(cfg, args.path)
    if command == "new":
        return cmd_new(cfg, args.name)
    parser.error(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
