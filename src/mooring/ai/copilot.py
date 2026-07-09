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

Auth: the SDK has no programmatic login, so we drive the Copilot CLI's
``copilot login`` (OAuth device flow) and reuse the stored credential via
``use_logged_in_user=True``. Older SDKs bundled a native ``copilot.exe`` inside
the package; github-copilot-sdk >=1.0.2 instead downloads it to a shared cache at
first use. Discovery (:meth:`CopilotProvider._cli_path`) checks
``COPILOT_CLI_PATH``, the legacy bundled location, the SDK's download cache, then
``PATH`` — and fetches it on demand for the login subprocess.
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
_COPILOT_UNAVAILABLE = "The Copilot CLI is not available in this build."


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
        self._models_error = ""  # why the last list_models() came back empty (a 403 etc.)

    # -- discovery ----------------------------------------------------------

    def _cli_path(self, *, download: bool = False) -> str | None:
        override = os.environ.get("COPILOT_CLI_PATH")
        if override and Path(override).exists():
            return override
        try:
            import copilot as copilot_sdk

            # Legacy layout: github-copilot-sdk <1.0.2 bundled the binary under
            # <pkg>/bin/. Newer SDKs download it at first use instead (below).
            bin_name = "copilot.exe" if sys.platform == "win32" else "copilot"
            candidate = Path(copilot_sdk.__file__).parent / "bin" / bin_name
            if candidate.exists():
                return str(candidate)
        except Exception:  # noqa: BLE001  # SDK not importable -> not available
            pass
        # github-copilot-sdk >=1.0.2 downloads the CLI to a shared cache at first
        # use rather than bundling it. Prefer an already-cached binary; only fetch
        # over the network when explicitly asked (the login subprocess needs a real
        # binary on disk) so available()/status stay cheap and offline-safe.
        try:
            from copilot import _cli_download

            cached = _cli_download.get_cached_cli_path()
            if cached and Path(cached).exists():
                return cached
            if download:
                fetched = _cli_download.get_or_download_cli()
                if fetched and Path(fetched).exists():
                    return fetched
        except Exception:  # noqa: BLE001  # private SDK API absent/changed -> fall through
            pass
        return shutil.which("copilot")

    def available(self) -> bool:
        try:
            import copilot  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        if self._cli_path() is not None:
            return True
        # github-copilot-sdk >=1.0.2 ships no bundled binary; it auto-downloads the
        # CLI at first use when a version is pinned (a published wheel). Treat that
        # as available so a fresh `mooring[copilot]` install works without a manual
        # pre-download — the binary lands lazily on connect/session start. (An
        # editable/source SDK pins CLI_VERSION=None and can't auto-download, so it
        # stays unavailable unless a binary is already resolvable above.)
        try:
            from copilot._cli_version import CLI_VERSION

            return CLI_VERSION is not None
        except Exception:  # noqa: BLE001
            return False

    def _require_cli(self) -> str:
        """Resolve the CLI binary for a direct ``copilot login`` subprocess.

        Unlike the SDK-client paths (probe/models/session), the login command
        shells out to the binary directly, so it can't rely on the SDK's own
        download-on-start. Fetch it on first use here, and raise a friendly
        :class:`AIError` if it can't be obtained (offline, or a source SDK with no
        pinned version) rather than tripping an assertion.
        """
        cli = self._cli_path(download=True)
        if cli is None:
            raise AIError(
                "Couldn't obtain the Copilot CLI binary (the SDK downloads it on "
                "first use). Check your network connection, or set COPILOT_CLI_PATH "
                "to a manually-installed binary."
            )
        return cli

    # -- status -------------------------------------------------------------

    def status(self, force: bool = False) -> ProviderStatus:
        if not self.available():
            return ProviderStatus(
                self.name,
                available=False,
                connected=False,
                detail=_COPILOT_UNAVAILABLE,
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
                detail=_COPILOT_UNAVAILABLE,
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
        except Exception as exc:  # noqa: BLE001  # report, never raise into a probe
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

    def connect(self, host: str | None = None) -> ProviderStatus:
        """Start ``copilot login`` (browser device flow) in the background.

        Returns immediately: the CLI opens a browser; the caller polls
        ``status``/``login_state`` and refreshes once the user has authorised.
        ``host`` targets a GitHub Enterprise Copilot instance (data residency),
        mirroring :meth:`login_interactive`.
        """
        if not self.available():
            raise AIError(_COPILOT_UNAVAILABLE)
        cli = self._require_cli()
        with self._login_lock:
            if self._login_proc is not None and self._login_proc.poll() is None:
                return self._connecting_status("Sign-in already in progress.")
            self._login_output = []
            cmd = [cli, "login"]
            if host:
                cmd += ["--host", host]
            try:
                proc = subprocess.Popen(
                    cmd,
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
        except Exception:  # noqa: BLE001  # draining is best-effort
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
            raise AIError(_COPILOT_UNAVAILABLE)
        cli = self._require_cli()
        cmd = [cli, "login"]
        if host:
            cmd += ["--host", host]
        result = subprocess.run(cmd)  # noqa: S603  # bundled trusted binary, inherits stdio
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
        semantic_models=None,
        helpers=None,
        pii: PiiConfig | None = None,
        traceback_guard: bool = True,
        background: bool = False,
    ):
        """Open a long-lived, streaming, value-blind chat session (the copilot).

        The session reuses :func:`hardened_session_kwargs` (the audited privacy
        config) and adds mooring's safe tools (plus the dictionary tools when
        ``dictionary`` is a non-empty index, and the Power BI semantic-model
        tools when ``semantic_models`` carries pre-parsed models).
        ``model``/``reasoning_effort`` override the configured defaults when given. ``traceback_guard`` (default
        ON, like the config key) arms the session's sanitise-and-hold valve for
        pasted tracebacks — it travels here like the ``pii`` config so a caller
        can't silently drop it.

        ``background=False`` (default) blocks until the session is ready and raises
        :class:`AIError` on a startup/auth/policy failure. ``background=True``
        returns the still-starting session immediately so the hub can return the
        chat-open response without waiting on the (CLI-spawning, networked)
        handshake; readiness/failure then arrives over the session's event stream.
        """
        if not self.available():
            raise AIError(
                "Copilot isn't available. Install the extra: pip install mooring[copilot]"
            )
        from mooring.ai import ner
        from mooring.ai.session import CopilotChatSession

        pii = pii or PiiConfig()
        # Resolve "auto" -> a concrete backend and shape name_model for it (GLiNER: a
        # pinned ModelRef; spaCy: a name/path string, "" = vendored model) ONLY when
        # the name pass will actually run (guard on AND names on) — so a default,
        # guard-off install never imports spaCy, which resolve_backend("auto") would,
        # just to open a chat. When names are off the raw config is passed through and
        # never used; when on, the session re-resolves nothing.
        backend = pii.name_backend
        name_model = pii.name_model
        if pii.enabled and pii.names:
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
            semantic_models=semantic_models,
            helpers=helpers,
            pii_enabled=pii.enabled,
            pii_block=pii.block_prompt,
            # NER name detection only acts when the whole guard is on.
            pii_names=pii.enabled and pii.names,
            pii_name_labels=pii.name_labels,
            pii_name_threshold=pii.name_threshold,
            pii_name_model=name_model,
            pii_name_backend=backend,
            traceback_guard=traceback_guard,
        )
        # Default path blocks (and raises on failure); the hub passes background=True
        # to return the open response immediately and stream readiness instead. The
        # no-arg call keeps older session stubs (def start(self)) working.
        if background:
            session.start(block=False)
        else:
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
            error = ""
        except Exception as exc:  # noqa: BLE001  # never raise into the hub; report via models_error
            models = []
            # KEEP the reason so the UI can TELL the user instead of silently showing
            # an empty model list. The common one is a 403 "not authorized to use this
            # Copilot feature" (wrong/unlicensed account) — friendly_error turns that
            # into "switch account or ask an admin". Value-free: a provider API error
            # string, never any data.
            error = friendly_error(str(exc))
        with self._models_lock:
            self._cached_models = models
            self._models_at = time.monotonic()
            self._models_error = error
        return models

    def models_error(self) -> str:
        """Why the last :meth:`list_models` returned no models — a provider API error
        such as a 403 'not authorized to use this Copilot feature' — or '' when the
        last fetch was clean. Lets the hub surface WHY the model list is empty (so the
        user can switch account or ask an admin) rather than show a dead dropdown.
        Empty when simply NOT SIGNED IN (that path returns [] without an error; the
        sign-in panel handles it)."""
        with self._models_lock:
            return self._models_error

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


def _deny_all(request, invocation):  # noqa: ANN001  # SDK permission callback
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
