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
    sub.add_parser("status", help="show sync status of workspace files")

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

    sub.add_parser("selftest", help="verify the bundled environment")
    sub.add_parser("version", help="print the version")
    return parser


def _print_paths(cfg: config.Config) -> None:
    print(f"  config file : {paths.user_config_file()}")
    print(f"  workspace   : {cfg.workspace()}")
    print(f"  logs        : {paths.user_log_dir()}")


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


def main(argv: list[str] | None = None) -> int:
    _ensure_child_pythonpath()
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "hub"
    cfg = config.load_config()

    if command == "version":
        print(f"mooring {__version__}")
        return 0
    if command == "selftest":
        return cmd_selftest(cfg)
    if command == "hub":
        from mooring.hub.server import run_hub

        no_browser = getattr(args, "no_browser", False)
        port = getattr(args, "port", None)
        return run_hub(cfg, open_browser=not no_browser, port=port)
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
