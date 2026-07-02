"""The diagnosis engine behind ``mooring doctor`` and the hub's health check.

Mooring's users are non-developers on locked-down Windows laptops; when a
rollout fails (TLS-intercepting proxy, revoked token, Python too old, uv
missing) the failure reaches the admin as "mooring is broken". Only mooring
holds the cross-artifact context — the delivery mode, the locked env, the sync
manifest, the GitHub host and token — so only mooring can say WHICH of the
usual suspects applies. Each probe reports pass / warn / fail / unknown with a
one-line curated fix, and the whole run renders into a **paste-safe report**:
probes emit curated strings (never raw exception dumps, which can embed full
URLs), and :func:`redact` collapses the home directory, the enterprise host,
and the OS username as a second, pinned-by-test line of defence.

Design rules: fewer, high-confidence probes (a false alarm erodes trust
faster than a missing check — anything uncertain reports ``unknown``, never
"broken"); diagnose, then point at EXISTING recoveries; never run at hub
startup (probes execute only on demand). The Copilot probe is deliberately
absent here — ``ai/`` sits above this module, so the adapters append it via
``extra_probes`` (the same adapter-orchestration pattern as the push guard).
"""

from __future__ import annotations

import getpass
import importlib
import importlib.util
import os
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import requests

from mooring import __version__, auth, config, githost, manifest, pyproject_env, runtime
from mooring.github import AuthFailed, GitHubClient, NotFound, RateLimited

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
_ICONS = {PASS: "ok  ", WARN: "warn", FAIL: "FAIL", UNKNOWN: "?   "}

# Reachability probe timeout: short so a dead proxy can't wedge the health check.
_REACH_TIMEOUT = 5


@dataclass(frozen=True)
class ProbeResult:
    id: str
    title: str
    status: str  # pass | warn | fail | unknown
    detail: str  # curated, human, value-free — never a raw exception dump
    fix: str = ""  # one line: what to do about it (empty when nothing to do)


def truststore_disabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether OS-trust-store TLS was explicitly turned off (MOORING_TRUSTSTORE=0).
    Lives here (not in cli.py) so the hub can consult it without importing the CLI."""
    env = os.environ if env is None else env
    return env.get("MOORING_TRUSTSTORE", "1").strip().lower() in ("0", "false", "no", "off")


# -- probes -------------------------------------------------------------------


def _probe_python(cfg: config.Config) -> ProbeResult:
    version = sys.version.split()[0]
    try:
        from mooring.editor import uses_uv

        mode = "uv project" if uses_uv(cfg.workspace()) else (
            "bundled environment (uv available)" if pyproject_env.uv_available()
            else "bundled environment (frozen build)"
        )
    except Exception:  # noqa: BLE001  # delivery detection must never sink the probe
        mode = "unknown delivery mode"
    if sys.version_info < (3, 12):
        return ProbeResult(
            "python", "Python runtime", FAIL,
            f"Python {version} — mooring needs 3.12 or newer.",
            "Install Python 3.12+ (or use the frozen build, which carries its own).",
        )
    return ProbeResult("python", "Python runtime", PASS, f"Python {version}, {mode}.")


def _probe_runtime_imports(_cfg: config.Config) -> ProbeResult:
    broken = []
    for name in runtime.SELFTEST_PACKAGES:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001  # the NAME is the diagnosis; the trace is noise
            broken.append(name)
    if broken:
        return ProbeResult(
            "runtime", "Bundled runtime", FAIL,
            f"Cannot import: {', '.join(broken)}.",
            "The install is damaged — reinstall mooring (or re-download the "
            "frozen build; antivirus sometimes quarantines extracted files).",
        )
    # The marimo subprocess and its kernels import the bundled stack via
    # PYTHONPATH (site.addsitedir does not inherit) — cli._ensure_child_pythonpath.
    spec = importlib.util.find_spec("marimo")
    if spec is not None and spec.origin:
        site_dir = str(Path(spec.origin).resolve().parents[1])
        parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
        if site_dir not in parts:
            return ProbeResult(
                "runtime", "Bundled runtime", WARN,
                "marimo's packages are not on PYTHONPATH for child processes.",
                "Start mooring via its normal entry point (`mooring hub`) so the "
                "notebook kernels can import the bundled stack.",
            )
    return ProbeResult(
        "runtime", "Bundled runtime",
        PASS, f"{len(runtime.SELFTEST_PACKAGES)} core packages import cleanly.",
    )


def _probe_github_reach(cfg: config.Config) -> ProbeResult:
    root = githost.api_root(cfg.host)
    trust = "off (MOORING_TRUSTSTORE=0)" if truststore_disabled() else "OS trust store"
    try:
        requests.get(root, timeout=_REACH_TIMEOUT)
    except requests.exceptions.SSLError:
        return ProbeResult(
            "reach", "GitHub reachability", FAIL,
            f"TLS failed against the GitHub API (trust: {trust}).",
            "This usually means a corporate proxy inspects TLS and its root CA "
            "isn't installed in the OS trust store — ask IT to install it "
            "(mooring verifies TLS against the OS store).",
        )
    except requests.exceptions.RequestException:
        return ProbeResult(
            "reach", "GitHub reachability", FAIL,
            "Cannot reach the GitHub API host.",
            "Check the network/VPN/proxy; if your team uses GitHub Enterprise, "
            "confirm the configured host is right (`mooring config get github.host`).",
        )
    return ProbeResult("reach", "GitHub reachability", PASS, "The GitHub API host answers.")


def _probe_github_auth(cfg: config.Config) -> ProbeResult:
    if not cfg.is_configured:
        return ProbeResult(
            "auth", "GitHub login & repo access", UNKNOWN,
            "No team repo configured — nothing to check.",
            "Connect one from the hub header (or `mooring repo add`).",
        )
    token = auth.get_token(host=cfg.host)
    if not token:
        return ProbeResult(
            "auth", "GitHub login & repo access", WARN,
            "Not logged in to GitHub.",
            "Run `mooring login` (or the hub's Log in button).",
        )
    # Fail fast on a dead network before the real calls: the sync client's
    # session auto-retries GETs with backoff, which is right for sync and wrong
    # for a health check — a black-holed connection would sit for minutes.
    try:
        requests.get(githost.api_root(cfg.host), timeout=_REACH_TIMEOUT)
    except requests.exceptions.RequestException:
        return ProbeResult(
            "auth", "GitHub login & repo access", UNKNOWN,
            "Skipped — the GitHub host is unreachable (see the reachability check).",
            "Fix the connection first, then re-run.",
        )
    # A plain session: no retry adapter, so the two calls below are bounded.
    client = GitHubClient(token, cfg.owner, cfg.repo, host=cfg.host, session=requests.Session())
    try:
        client.get_user()
        client.get_branch_head(cfg.branch)
    except AuthFailed:
        return ProbeResult(
            "auth", "GitHub login & repo access", FAIL,
            "GitHub rejected the stored login (expired or revoked).",
            "Log in again: `mooring login`.",
        )
    except NotFound:
        return ProbeResult(
            "auth", "GitHub login & repo access", FAIL,
            "Logged in, but the team repo or its branch is not accessible.",
            "Ask your admin for access to the repo (or check the branch name).",
        )
    except RateLimited:
        return ProbeResult(
            "auth", "GitHub login & repo access", WARN,
            "GitHub is rate-limiting this account right now.",
            "Wait a few minutes and try again.",
        )
    except requests.exceptions.RequestException:
        return ProbeResult(
            "auth", "GitHub login & repo access", UNKNOWN,
            "Could not complete the check (network hiccup).",
            "Re-run the health check once the connection is stable.",
        )
    return ProbeResult(
        "auth", "GitHub login & repo access", PASS, "Logged in; the team repo answers.",
    )


def _probe_config_files(cfg: config.Config) -> ProbeResult:
    problems: list[tuple[str, str]] = []
    try:
        config.load_app_config()
    except ValueError:
        problems.append((
            "your user config.toml is invalid",
            "fix it or `mooring config path` to find and edit it",
        ))
    workspace = cfg.workspace()
    try:
        manifest.load(workspace)
    except Exception:  # noqa: BLE001  # any parse/IO failure means the same fix
        problems.append((
            "the sync manifest is corrupt",
            "delete <workspace>/.mooring/manifest.json and run a pull to rebuild it",
        ))
    shared = workspace / "mooring.toml"
    if shared.is_file():
        try:
            tomllib.loads(shared.read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            problems.append((
                "the shared mooring.toml is not valid TOML",
                "fix it (it is a synced team file — coordinate before editing)",
            ))
    if problems:
        return ProbeResult(
            "configs", "Config & manifest files", FAIL,
            "; ".join(p[0] for p in problems) + ".",
            "; ".join(p[1] for p in problems) + ".",
        )
    return ProbeResult("configs", "Config & manifest files", PASS, "All parse cleanly.")


def _probe_deps_lock(cfg: config.Config) -> ProbeResult:
    workspace = cfg.workspace()
    pyproject = workspace / pyproject_env.PYPROJECT_NAME
    if not pyproject.is_file():
        return ProbeResult(
            "deps", "Notebook dependencies", PASS,
            "No notebook project — notebooks use the bundled environment.",
        )
    lock = workspace / "uv.lock"
    if not lock.is_file():
        return ProbeResult(
            "deps", "Notebook dependencies", WARN,
            "pyproject.toml has no uv.lock beside it.",
            "Run `mooring deps lock` and push, so everyone runs the same versions.",
        )
    try:
        if pyproject.stat().st_mtime > lock.stat().st_mtime:
            return ProbeResult(
                "deps", "Notebook dependencies", WARN,
                "pyproject.toml changed after uv.lock was written (possibly stale).",
                "Run `mooring deps lock` to refresh it (harmless if already current).",
            )
    except OSError:
        pass
    missing = []
    try:
        from mooring.editor import uses_uv

        if not uses_uv(workspace):
            missing = pyproject_env.missing_deps(workspace)
    except Exception:  # noqa: BLE001  # a probe must never raise
        pass
    if missing:
        return ProbeResult(
            "deps", "Notebook dependencies", WARN,
            f"This build can't provide: {', '.join(missing)} (declared, not bundled).",
            "Ask your admin to include them in the next build, or run mooring via uv.",
        )
    return ProbeResult("deps", "Notebook dependencies", PASS, "Project and lock look consistent.")


def _probe_workspace(cfg: config.Config) -> ProbeResult:
    hint = runtime.workspace_hint(cfg)
    if hint:
        return ProbeResult("workspace", "Workspace placement", WARN, hint)
    return ProbeResult("workspace", "Workspace placement", PASS, "No placement concerns.")


_PROBES: tuple[Callable[[config.Config], ProbeResult], ...] = (
    _probe_python,
    _probe_runtime_imports,
    _probe_config_files,
    _probe_github_reach,
    _probe_github_auth,
    _probe_deps_lock,
    _probe_workspace,
)


def run_probes(
    cfg: config.Config,
    extra_probes: Iterable[Callable[[], ProbeResult]] = (),
) -> list[ProbeResult]:
    """Run every probe, never raising: a probe that blows up reports itself as
    ``unknown`` (with a re-run hint) rather than sinking the whole check."""
    results: list[ProbeResult] = []
    for probe in _PROBES:
        try:
            results.append(probe(cfg))
        except Exception:  # noqa: BLE001  # the check must always complete
            results.append(
                ProbeResult(
                    probe.__name__.removeprefix("_probe_"), "Diagnostic probe", UNKNOWN,
                    "This check could not run.", "Re-run the health check.",
                )
            )
    for extra in extra_probes:
        try:
            results.append(extra())
        except Exception:  # noqa: BLE001
            results.append(
                ProbeResult(
                    "extra", "Diagnostic probe", UNKNOWN,
                    "This check could not run.", "Re-run the health check.",
                )
            )
    return results


def render_lines(results: list[ProbeResult]) -> list[str]:
    """The plain-text house style (shadow.warning_lines / `mooring ai pii doctor`)."""
    lines = []
    for r in results:
        lines.append(f"  {_ICONS.get(r.status, '?   ')} {r.title}: {r.detail}")
        if r.fix and r.status != PASS:
            lines.append(f"       fix: {r.fix}")
    return lines


def redact(text: str, cfg: config.Config) -> str:
    """Make report text safe to paste into a ticket: collapse the home directory,
    the enterprise GitHub host, the org/repo names, and the OS username. Second
    line of defence — probes already emit curated strings, never raw dumps."""
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    if cfg.host and cfg.host != githost.DEFAULT_HOST:
        text = text.replace(cfg.host, "<github-host>")
    # Org/repo names identify the customer; workspace-path hints end in them.
    for value, placeholder in ((cfg.owner, "<owner>"), (cfg.repo, "<repo>")):
        if value and len(value) > 2:
            text = text.replace(value, placeholder)
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001  # no login name — nothing to redact
        user = ""
    if user and len(user) > 2:
        text = text.replace(user, "<user>")
    return text


def build_report(results: list[ProbeResult], cfg: config.Config) -> str:
    """The paste-safe "Copy report" text: version + platform + redacted lines."""
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL, UNKNOWN)}
    header = (
        f"mooring doctor report (mooring {__version__}, "
        f"python {sys.version.split()[0]}, {sys.platform})\n"
        f"{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, "
        f"{counts[UNKNOWN]} unknown\n"
    )
    return redact(header + "\n".join(render_lines(results)) + "\n", cfg)
