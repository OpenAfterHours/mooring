"""GitHub Copilot provider, built on the official ``github-copilot-sdk``.

The SDK is an *agent* that can normally read files and run shell commands. For a
financial-data privacy feature that is exactly what we must forbid, so every
generation runs the agent with tools fully disabled — three independent layers,
all verified against the installed SDK (v1.0.1):

1. ``available_tools=[]``      — an empty allowlist, so no tool is ever offered.
2. ``on_permission_request``  — a deny-all handler that rejects any tool request.
3. fail-closed by default     — the SDK denies if the handler errors/returns nothing.

plus hardening flags that switch off skills, file hooks, custom instructions,
host git, session telemetry, the on-disk session store (so the schema and
instruction are not persisted to ~/.copilot), embedding retrieval, and local
config discovery. The agent therefore can only read the schema text we hand it;
it can never reach the data files.

Auth: the SDK has no programmatic login, so we drive the bundled CLI's
``copilot login`` (OAuth device flow) and reuse the stored credential via
``use_logged_in_user=True``. The bundled CLI is a native ``copilot.exe`` shipped
inside the package; discovery is path-based and survives moonlit's extraction.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from mooring.ai import prompt
from mooring.ai.base import AIError, ProviderStatus

_GENERATE_TIMEOUT = 120.0  # seconds the model may take to answer
_PROBE_TIMEOUT = 30.0  # seconds to check sign-in status
_STATUS_TTL = 45.0  # cache the (CLI-spawning) auth probe this long
_MAX_LOGIN_LINES = 50


class CopilotProvider:
    name = "copilot"

    def __init__(self, model: str = "") -> None:
        self.model = (model or "").strip()
        self._login_lock = threading.Lock()
        self._login_proc: subprocess.Popen | None = None
        self._login_output: list[str] = []
        self._cache_lock = threading.Lock()
        self._cached_status: ProviderStatus | None = None
        self._cached_at = 0.0

    # -- discovery ----------------------------------------------------------

    def _cli_path(self) -> str | None:
        override = os.environ.get("COPILOT_CLI_PATH")
        if override and Path(override).exists():
            return override
        try:
            import copilot as copilot_sdk

            bin_name = "copilot.exe" if sys.platform == "win32" else "copilot"
            candidate = Path(copilot_sdk.__file__).parent / "bin" / bin_name
            if candidate.exists():
                return str(candidate)
        except Exception:  # noqa: BLE001 - SDK not importable -> not available
            pass
        return shutil.which("copilot")

    def available(self) -> bool:
        try:
            import copilot  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return self._cli_path() is not None

    # -- status -------------------------------------------------------------

    def status(self, force: bool = False) -> ProviderStatus:
        if not self.available():
            return ProviderStatus(
                self.name,
                available=False,
                connected=False,
                detail="The Copilot CLI is not available in this build.",
            )
        with self._cache_lock:
            fresh = (
                self._cached_status is not None
                and (time.monotonic() - self._cached_at) < _STATUS_TTL
            )
            if fresh and not force:
                return self._cached_status
        status = self._probe()
        with self._cache_lock:
            self._cached_status = status
            self._cached_at = time.monotonic()
        return status

    def cached_status(self) -> ProviderStatus | None:
        """The last known status without spawning the CLI. None = never probed.

        Used on the auto-loaded hub state so opening the hub never starts the
        150 MB CLI; an explicit Check/Connect/Generate does the real probe.
        """
        if not self.available():
            return ProviderStatus(
                self.name,
                available=False,
                connected=False,
                detail="The Copilot CLI is not available in this build.",
            )
        with self._cache_lock:
            if (
                self._cached_status is not None
                and (time.monotonic() - self._cached_at) < _STATUS_TTL
            ):
                return self._cached_status
        return None

    def _probe(self) -> ProviderStatus:
        try:
            authed, login = self._run(self._aauth(), _PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - report, never raise into a probe
            return ProviderStatus(
                self.name,
                available=True,
                connected=False,
                detail=f"Couldn't check Copilot sign-in: {exc}",
            )
        if authed:
            return ProviderStatus(
                self.name,
                available=True,
                connected=True,
                account=login,
                detail=f"Connected{(' as ' + login) if login else ''}.",
            )
        return ProviderStatus(
            self.name,
            available=True,
            connected=False,
            detail="Not connected. Click Connect or run `mooring ai login`.",
        )

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_status = None
            self._cached_at = 0.0

    def _store_status(self, status: ProviderStatus) -> None:
        with self._cache_lock:
            self._cached_status = status
            self._cached_at = time.monotonic()

    # -- sign-in ------------------------------------------------------------

    def connect(self) -> ProviderStatus:
        """Start ``copilot login`` (browser device flow) in the background.

        Returns immediately: the CLI opens a browser; the caller polls
        ``status``/``login_state`` and refreshes once the user has authorised.
        """
        if not self.available():
            raise AIError("The Copilot CLI is not available in this build.")
        cli = self._cli_path()
        with self._login_lock:
            if self._login_proc is not None and self._login_proc.poll() is None:
                return self._connecting_status("Sign-in already in progress.")
            self._login_output = []
            try:
                proc = subprocess.Popen(
                    [cli, "login"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                raise AIError(f"Could not start Copilot sign-in: {exc}") from exc
            self._login_proc = proc
            threading.Thread(target=self._drain_login, args=(proc,), daemon=True).start()
        self._invalidate_cache()
        return self._connecting_status(
            "A browser window should open to sign in to Copilot. "
            "Authorise there, then click Refresh."
        )

    def _connecting_status(self, detail: str) -> ProviderStatus:
        return ProviderStatus(self.name, available=True, connected=False, detail=detail)

    def _drain_login(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        with self._login_lock:
                            self._login_output.append(line)
                            del self._login_output[:-_MAX_LOGIN_LINES]
            proc.wait()
        except Exception:  # noqa: BLE001 - draining is best-effort
            pass
        finally:
            self._invalidate_cache()

    def login_state(self) -> dict:
        with self._login_lock:
            running = self._login_proc is not None and self._login_proc.poll() is None
            return {"running": running, "output": list(self._login_output)}

    def login_interactive(self, host: str | None = None) -> int:
        """Run ``copilot login`` attached to the terminal (the CLI command path)."""
        if not self.available():
            raise AIError("The Copilot CLI is not available in this build.")
        cmd = [self._cli_path(), "login"]
        if host:
            cmd += ["--host", host]
        result = subprocess.run(cmd)  # noqa: S603 - bundled trusted binary, inherits stdio
        self._invalidate_cache()
        return result.returncode

    # -- generation ---------------------------------------------------------

    def generate(
        self, *, schema_context: str, instruction: str, target: str = "polars"
    ) -> str:
        if not self.available():
            raise AIError("The Copilot CLI is not available in this build.")
        if not instruction.strip():
            raise AIError("Describe what you want the code to do.")
        system, user = prompt.build_messages(
            schema_context=schema_context, instruction=instruction, target=target
        )
        try:
            text = self._run(
                self._agenerate(system, user, self.model), _GENERATE_TIMEOUT + 45
            )
        except AIError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise AIError("Copilot timed out. Try a simpler request or try again.") from exc
        except Exception as exc:  # noqa: BLE001 - surface a clean message to the UI
            raise AIError(_friendly_error(str(exc))) from exc
        # A successful call proves we're connected — refresh the cache positively
        # so the card doesn't flip to "not connected" on the next state poll.
        prev = self._cached_status
        account = prev.account if prev else ""
        self._store_status(
            ProviderStatus(
                self.name,
                available=True,
                connected=True,
                account=account,
                detail=f"Connected{(' as ' + account) if account else ''}.",
            )
        )
        return _extract_code(text)

    async def _agenerate(self, system: str, user: str, model: str) -> str:
        from copilot import CopilotClient
        from copilot.rpc import PermissionDecisionReject
        from copilot.session_events import AssistantMessageData

        def deny_all(request, invocation):  # noqa: ANN001 - SDK callback
            return PermissionDecisionReject(
                feedback="Tool use is disabled by mooring's data-privacy policy."
            )

        client = CopilotClient(use_logged_in_user=True)
        async with client:
            auth = await client.get_auth_status()
            if not _is_authed(auth):
                raise AIError("Copilot isn't connected. Run `mooring ai login` to sign in.")
            session = await client.create_session(
                model=model or None,
                on_permission_request=deny_all,
                available_tools=[],  # no tools => the agent can never read a file
                system_message={"mode": "append", "content": system},
                enable_session_telemetry=False,
                skip_custom_instructions=True,
                enable_skills=False,
                enable_file_hooks=False,
                enable_host_git_operations=False,
                # Don't persist the schema/instruction/generated code to ~/.copilot,
                # and don't read local config — these are off only by default in
                # 'empty' mode, so set them explicitly in the default mode too.
                enable_session_store=False,
                skip_embedding_retrieval=True,
                enable_config_discovery=False,
            )
            async with session:
                event = await session.send_and_wait(user, timeout=_GENERATE_TIMEOUT)
                if event is not None and isinstance(event.data, AssistantMessageData):
                    return event.data.content or ""
                return ""

    async def _aauth(self) -> tuple[bool, str]:
        from copilot import CopilotClient

        client = CopilotClient(use_logged_in_user=True)
        async with client:
            auth = await client.get_auth_status()
            return _is_authed(auth), _login_of(auth)

    @staticmethod
    def _run(coro, timeout: float):
        return asyncio.run(asyncio.wait_for(coro, timeout))


def _is_authed(auth: object) -> bool:
    for attr in ("isAuthenticated", "is_authenticated", "authenticated"):
        if hasattr(auth, attr):
            return bool(getattr(auth, attr))
    return False


def _login_of(auth: object) -> str:
    for attr in ("login", "user", "username"):
        value = getattr(auth, attr, None)
        if value:
            return str(value)
    return ""


def _friendly_error(msg: str) -> str:
    low = msg.lower()
    if "not authorized" in low or "policy" in low or "enterprise or organization" in low:
        return (
            "Copilot rejected the request: this account isn't authorized for the "
            "Copilot SDK/agent feature. A GitHub org/enterprise admin must enable "
            "the relevant Copilot policy (CLI / agent access) for your organization."
        )
    return f"Copilot request failed: {msg}"


def _extract_code(text: str) -> str:
    """Pull the code out of the first fenced block so it pastes straight in."""
    text = (text or "").strip()
    if "```" not in text:
        return text
    after = text.split("```", 1)[1]
    # drop an optional language tag on the opening fence line
    if "\n" in after:
        first, rest = after.split("\n", 1)
        if first.strip().isalpha():
            after = rest
    return after.split("```", 1)[0].strip()
