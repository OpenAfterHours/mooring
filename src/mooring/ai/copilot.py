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

from mooring.ai.base import AIError, ProviderStatus
from mooring.ai_config import PiiConfig

_PROBE_TIMEOUT = 30.0  # seconds to check sign-in status
_STATUS_TTL = 45.0  # cache the (CLI-spawning) auth probe this long
_MODELS_TTL = 300.0  # cache the model list this long (it changes rarely)
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
        self._models_lock = threading.Lock()
        self._cached_models: list[dict] | None = None
        self._models_at = 0.0

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

    def open_chat(
        self,
        *,
        system_context: str,
        workspace,
        folders,
        notebook_rel: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        dictionary=None,
        pii: PiiConfig | None = None,
    ):
        """Open a long-lived, streaming, value-blind chat session (the copilot).

        The session reuses :func:`hardened_session_kwargs` (the audited privacy
        config) and adds mooring's safe tools (plus the dictionary tools when
        ``dictionary`` is a non-empty index). ``model``/``reasoning_effort``
        override the configured defaults when given. Raises :class:`AIError` on a
        startup/auth/policy failure.
        """
        if not self.available():
            raise AIError(
                "Copilot isn't available. Install the extra: pip install mooring[copilot]"
            )
        from mooring.ai import ner
        from mooring.ai.session import CopilotChatSession

        pii = pii or PiiConfig()
        # Resolve "auto" -> a concrete backend ONCE here, then shape name_model for it
        # (GLiNER: a pinned ModelRef; spaCy: a name/path string, "" = vendored model)
        # and pass the concrete backend down, so the session never re-resolves.
        backend = ner.resolve_backend(pii.name_backend)
        name_model = ner.model_for(backend, pii.name_model, pii.name_revision, pii.name_variant)
        session = CopilotChatSession(
            model=(model or "").strip() or self.model,
            reasoning_effort=reasoning_effort,
            system_context=system_context,
            workspace=workspace,
            folders=folders,
            notebook_rel=notebook_rel,
            dictionary=dictionary,
            pii_enabled=pii.enabled,
            pii_block=pii.block_prompt,
            # NER name detection only acts when the whole guard is on.
            pii_names=pii.enabled and pii.names,
            pii_name_labels=pii.name_labels,
            pii_name_threshold=pii.name_threshold,
            pii_name_model=name_model,
            pii_name_backend=backend,
        )
        session.start()
        return session

    # -- models -------------------------------------------------------------

    def list_models(self, force: bool = False) -> list[dict]:
        """The models the signed-in user may use, as value-free dicts.

        Returns ``[]`` when unavailable / not signed in (the caller reports the
        connection state separately). Cached for ``_MODELS_TTL`` since it spawns
        the CLI. Each dict: id, name, efforts (the model's supported reasoning
        efforts), default_effort, multiplier (premium-request cost or None).
        """
        if not self.available():
            return []
        with self._models_lock:
            fresh = (
                self._cached_models is not None
                and (time.monotonic() - self._models_at) < _MODELS_TTL
            )
            if fresh and not force:
                return self._cached_models
        try:
            models = self._run(self._alist_models(), _PROBE_TIMEOUT)
        except Exception:  # noqa: BLE001 - never raise into the hub; report none
            models = []
        with self._models_lock:
            self._cached_models = models
            self._models_at = time.monotonic()
        return models

    async def _alist_models(self) -> list[dict]:
        from copilot import CopilotClient

        client = CopilotClient(use_logged_in_user=True)
        async with client:
            auth = await client.get_auth_status()
            if not is_authed(auth):
                return []
            return [_model_dict(m) for m in await client.list_models()]

    async def _aauth(self) -> tuple[bool, str]:
        from copilot import CopilotClient

        client = CopilotClient(use_logged_in_user=True)
        async with client:
            auth = await client.get_auth_status()
            return is_authed(auth), _login_of(auth)

    @staticmethod
    def _run(coro, timeout: float):
        return asyncio.run(asyncio.wait_for(coro, timeout))


def _model_dict(m: object) -> dict:
    """Serialize a Copilot ModelInfo to a plain, value-free dict for the UI.

    Defensive: an unexpected billing/effort shape must not break the listing.
    """
    billing = getattr(m, "billing", None)
    multiplier = getattr(billing, "multiplier", None) if billing is not None else None
    efforts = getattr(m, "supported_reasoning_efforts", None) or []
    return {
        "id": str(getattr(m, "id", "") or ""),
        "name": str(getattr(m, "name", "") or getattr(m, "id", "") or ""),
        "efforts": [str(e) for e in efforts],
        "default_effort": str(getattr(m, "default_reasoning_effort", "") or ""),
        "multiplier": multiplier,
    }


def _deny_all(request, invocation):  # noqa: ANN001 - SDK permission callback
    """Reject every permission request: a fail-closed backstop behind the
    available_tools allowlist (so a built-in that asks permission can't run)."""
    from copilot.rpc import PermissionDecisionReject

    return PermissionDecisionReject(
        feedback="Tool use is restricted by mooring's data-privacy policy."
    )


def hardened_session_kwargs(system_context: str) -> dict:
    """The audited, value-blind ``create_session`` options shared by every session.

    THE PRIVACY CHOKE POINT for the Copilot session: a deny-all permission
    backstop, and nothing about the conversation persisted or used to discover
    local config / skills / git / file hooks. Callers add model / streaming /
    tools / available_tools / working_directory.
    """
    return {
        "on_permission_request": _deny_all,
        "system_message": {"mode": "append", "content": system_context},
        "enable_session_telemetry": False,
        "skip_custom_instructions": True,
        "enable_skills": False,
        "enable_file_hooks": False,
        "enable_host_git_operations": False,
        "enable_session_store": False,
        "skip_embedding_retrieval": True,
        "enable_config_discovery": False,
    }


def is_authed(auth: object) -> bool:
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


def friendly_error(msg: str) -> str:
    low = msg.lower()
    if "not authorized" in low or "policy" in low or "enterprise or organization" in low:
        return (
            "Copilot rejected the request: this account isn't authorized for the "
            "Copilot SDK/agent feature. A GitHub org/enterprise admin must enable "
            "the relevant Copilot policy (CLI / agent access) for your organization."
        )
    return f"Copilot request failed: {msg}"
