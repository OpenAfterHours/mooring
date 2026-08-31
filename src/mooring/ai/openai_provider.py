"""OpenAI provider, built on the official ``openai`` SDK (the ``mooring[openai]`` extra).

A value-blind alternative to the GitHub Copilot backend. Unlike Copilot's SDK —
an autonomous agent that runs the tool loop and ships built-in file/shell tools —
the OpenAI **Chat Completions** API is stateless message-in / message-out and has
NO hosted tools: its ``tools=`` argument accepts function specs only. That makes
value-blindness a property of the API surface (there is no ``web_search`` /
``file_search`` / ``code_interpreter`` the model could reach data through), so the
copilot's allowlist + deny-all-permission + empty-working-dir hardening collapses
to one rule here — mooring only ever registers its own value-free function tools
(:func:`mooring.ai.tools.build_openai_tools`). The multi-turn tool-calling loop
mooring must run itself lives in :class:`mooring.ai.openai_session.OpenAIChatSession`.

Auth is a static API key, not an OAuth device flow: it is resolved LOCALLY only —
``MOORING_OPENAI_API_KEY`` (mirroring ``MOORING_TOKEN``) → the OS keyring →
``OPENAI_API_KEY`` — and NEVER read from the synced ``mooring.toml`` (a teammate's
key must not travel with the repo). ``base_url`` / ``api_version`` are the only
config knobs, and they are value-free: they point the client at an OpenAI-compatible
gateway or an Azure resource so an enterprise can keep data in its own tenant.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping

from mooring.ai.base import AIError, AINotConnectedError, ProviderStatus

_STATUS_TTL = 45.0  # cache a (possibly network-validating) status probe this long
_MODELS_TTL = 300.0  # cache the model list this long
_CLIENT_TIMEOUT = 30.0  # bound every OpenAI HTTP call so a hung gateway can't wedge us
_NO_KEY_DETAIL = (
    "No API key or endpoint configured. Set MOORING_OPENAI_API_KEY (or OPENAI_API_KEY), "
    "run `mooring ai key set`, or set a base URL for a keyless endpoint (e.g. a local server)."
)
# Sent to a keyless base_url endpoint (local vLLM/Ollama/LM Studio): the SDK still
# needs SOME api_key string even when the server ignores it.
_PLACEHOLDER_KEY = "not-needed"
_OPENAI_UNAVAILABLE = (
    "The OpenAI SDK isn't installed. Install the extra: pip install mooring[openai]"
)

KEYRING_SERVICE = "mooring-openai"
# A SEPARATE credential slot for the customer-data route. Distinct from
# KEYRING_SERVICE on purpose: the two endpoints are different destinations, and the
# whole point of the trusted route is that its credential is not the general one.
KEYRING_SERVICE_TRUSTED = "mooring-openai-trusted"
KEYRING_USER = "default"
_NO_TRUSTED_KEY_DETAIL = (
    "The approved AI route has no dedicated credential. "
    "Set MOORING_AI_TRUSTED_API_KEY in the managed launch environment."
)

# Chat-capable model id prefixes for the listing filter; models.list also returns
# embeddings / tts / whisper / image / moderation ids that are not chat models.
_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5", "chatgpt")
_NON_CHAT_MARKERS = (
    "embedding",
    "tts",
    "whisper",
    "audio",
    "image",
    "dall-e",
    "moderation",
    "realtime",
    "transcribe",
    "search",  # e.g. *-search-preview endpoints are not general chat
)

# The reasoning-effort choices advertised for a reasoning-capable model. OpenAI's
# listing carries no per-model effort metadata, so this is a fixed advisory list.
# "default" is a SENTINEL, not an API value: it means "send no reasoning_effort at
# all" (the session normalises it away). It is FIRST on purpose — ChatCore.chooseEffort
# falls back to ``efforts[0]`` when neither a stored pick nor a configured default is
# selectable, so merely making the picker visible keeps a FRESH user's requests
# byte-for-byte as today. This list is deliberately config-blind: a configured
# ``ai.reasoning_effort`` outside it (``minimal``, ``xhigh``, a gateway's own value)
# is unioned in by the hub route that serves the listing (hub/routes/chat.py), which
# is the only layer that knows the user's config.
_REASONING_EFFORTS = ("default", "none", "low", "medium", "high")


def _keyring():
    try:
        import keyring
        import keyring.errors  # noqa: F401

        if keyring.get_keyring() is None:
            return None
        return keyring
    except Exception:  # pragma: no cover - environment-dependent
        return None


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The OpenAI API key from LOCAL sources only, in precedence order.

    ``MOORING_OPENAI_API_KEY`` (mirrors ``MOORING_TOKEN`` — beats everything) → the
    OS keyring → ``OPENAI_API_KEY`` (the SDK's own env, for convenience). Never the
    synced ``mooring.toml``. Returns ``None`` when no key is configured.
    """
    env = os.environ if env is None else env
    key = env.get("MOORING_OPENAI_API_KEY")
    if key:
        return key.strip() or None
    kr = _keyring()
    if kr is not None:
        try:
            stored = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
            if stored:
                return stored
        except Exception:  # pragma: no cover - backend-dependent
            pass
    key = env.get("OPENAI_API_KEY")
    return (key.strip() or None) if key else None


def resolve_trusted_api_key(
    env: Mapping[str, str] | None = None, *, allow_keyring: bool = False
) -> str | None:
    """Resolve only the dedicated customer-data credential.

    There is deliberately no fallback to the user's general OpenAI key, in either
    profile: a deployment cannot accidentally send customer information with a
    credential meant for a different tenant or endpoint.

    ``allow_keyring`` is the ONE difference between the two profiles, and the caller
    passes it from the resolved routing source rather than deciding for itself:

    * a **managed** profile stays env-only, exactly as ``docs/admins/`` documents —
      the firm's credential is supplied by the launcher, and a key an analyst happens
      to have stored locally must not be able to satisfy it;
    * a **self-configured** profile has no launcher, so its key comes from the OS
      credential store under :data:`KEYRING_SERVICE_TRUSTED`.
    """
    env = os.environ if env is None else env
    key = env.get("MOORING_AI_TRUSTED_API_KEY")
    if key:
        return key.strip() or None
    if not allow_keyring:
        return None
    kr = _keyring()
    if kr is not None:
        try:
            stored = kr.get_password(KEYRING_SERVICE_TRUSTED, KEYRING_USER)
            if stored:
                return stored
        except Exception:  # pragma: no cover - backend-dependent
            pass
    return None


def save_trusted_api_key(key: str) -> None:
    """Store the self-configured customer-data key in the OS credential store."""
    kr = _keyring()
    if kr is None:  # pragma: no cover - environment-dependent
        raise AIError(
            "No OS credential store is available to store the key. "
            "Set MOORING_AI_TRUSTED_API_KEY in your environment instead."
        )
    kr.set_password(KEYRING_SERVICE_TRUSTED, KEYRING_USER, key.strip())


def delete_trusted_api_key() -> None:
    kr = _keyring()
    if kr is None:  # pragma: no cover - environment-dependent
        return
    try:
        kr.delete_password(KEYRING_SERVICE_TRUSTED, KEYRING_USER)
    except Exception:  # pragma: no cover - backend-dependent (absent == deleted)
        pass


def save_api_key(key: str) -> None:
    """Store the API key in the OS credential store (used by ``mooring ai key set``)."""
    kr = _keyring()
    if kr is None:  # pragma: no cover - environment-dependent
        raise AIError(
            "No OS credential store is available to store the key. "
            "Set MOORING_OPENAI_API_KEY in your environment instead."
        )
    kr.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())


def delete_api_key() -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:  # pragma: no cover - nothing stored / backend-dependent
        pass


_AUTH_MARKERS = ("401", "unauthorized", "invalid api key", "api key")
_RATE_MARKERS = ("429", "rate limit", "quota")
_EFFORT_FIELDS = ("reasoning_effort", "reasoning effort")
# A MENTION of the parameter is not a rejection of it: an error body routinely echoes
# the request's parameters, so a 401 or a 429 can name reasoning_effort while being
# about something else entirely. Require a word that says the server refused it.
_EFFORT_REJECTIONS = (
    "unsupported",
    "not supported",
    "does not support",
    "unrecognized",
    "unrecognised",
    "unknown",
    "unexpected",
    "invalid",
    "not permitted",
    "not allowed",
    "cannot be used",
    "must be",
)
_AUTH_HELP = (
    "OpenAI rejected the request: the API key is missing or invalid. "
    "Check MOORING_OPENAI_API_KEY / OPENAI_API_KEY (or your base_url)."
)
_RATE_HELP = "OpenAI rate-limited the request or the account is out of quota."
_EFFORT_HELP = (
    "OpenAI rejected the reasoning effort: this model may not accept one alongside "
    "function tools, which mooring always sends. Set the 'effort' picker beside the "
    "model — in this chat window, or on the batch page — to 'default' (send none) or "
    "'none'. That picker is what decides: it is remembered per provider and OVERRIDES "
    "Settings -> 'Default reasoning effort' (config ai.reasoning_effort, env "
    "MOORING_AI_REASONING_EFFORT), so clearing the setting alone will not change it."
)


def _status_code(low: str) -> str:
    """The HTTP status the SDK prefixes its message with ("Error code: 401 - ..."),
    or "" when the message carries none (a gateway may raise a bare error)."""
    match = re.search(r"error code:\s*(\d{3})", low)
    return match.group(1) if match else ""


def _has(low: str, markers: tuple[str, ...]) -> bool:
    return any(marker in low for marker in markers)


def friendly_error(msg: str) -> str:
    """Map a raw SDK/gateway error string onto one short, actionable line.

    Order is load-bearing. The STATUS CODE decides first where the SDK gives one,
    because an error body echoes the request's parameters — a 401 whose body lists
    the request keys, or a 429 reading "Rate limit reached for gpt-5 (params:
    reasoning_effort=high)", must be reported as auth and rate-limit faults, not as
    a rejected effort. The effort branch comes last and needs BOTH the parameter
    name and a rejection word; the substring auth/rate checks below it still catch a
    code-less message, and the original text is always preserved.
    """
    low = msg.lower()
    code = _status_code(low)
    if code == "401" or (not code and _has(low, _AUTH_MARKERS)):
        return _AUTH_HELP
    if code == "429" or (not code and _has(low, _RATE_MARKERS)):
        return _RATE_HELP
    if _has(low, _EFFORT_FIELDS) and _has(low, _EFFORT_REJECTIONS):
        return f"{_EFFORT_HELP} (OpenAI said: {msg})"
    # A status the branches above don't claim (a 400, a 5xx), so fall back to the
    # substring reading of the body.
    if _has(low, _AUTH_MARKERS):
        return _AUTH_HELP
    if _has(low, _RATE_MARKERS):
        return _RATE_HELP
    return f"OpenAI request failed: {msg}"


def build_client(
    api_key: str,
    *,
    base_url: str = "",
    api_version: str = "",
    timeout: float,
    follow_redirects: bool = True,
):
    """Construct the sync OpenAI client for the resolved key + endpoint.

    ``api_version`` set → a classic Azure deployment (``AzureOpenAI`` with
    ``azure_endpoint``); otherwise the standard client, with ``base_url`` pointing
    at an OpenAI-compatible gateway / Azure v1 endpoint when given.
    """
    import openai

    http_client = None
    if not follow_redirects:
        # A trust profile pins one HTTPS host. Following a gateway redirect to a
        # different host would silently widen that approval.
        http_client = openai.DefaultHttpxClient(follow_redirects=False)
    if api_version:
        kwargs = {
            "api_key": api_key,
            "azure_endpoint": base_url or None,
            "api_version": api_version,
            "timeout": timeout,
        }
        if http_client is not None:
            kwargs["http_client"] = http_client
        return openai.AzureOpenAI(**kwargs)
    kwargs = {"api_key": api_key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    if http_client is not None:
        kwargs["http_client"] = http_client
    return openai.OpenAI(**kwargs)


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        api_version: str = "",
        *,
        api_key_resolver: Callable[[], str | None] | None = None,
        require_api_key: bool = False,
        follow_redirects: bool = True,
        name: str = "openai",
    ) -> None:
        self.model = (model or "").strip()
        self._base_url = (base_url or "").strip()
        self._api_version = (api_version or "").strip()
        self._api_key_resolver = api_key_resolver or resolve_api_key
        self._require_api_key = bool(require_api_key)
        self._follow_redirects = bool(follow_redirects)
        self.name = name
        self._cached_status: ProviderStatus | None = None
        self._cached_at = 0.0
        self._cached_models: list[dict] | None = None
        self._models_at = 0.0
        self._models_error = ""

    # -- availability / auth -------------------------------------------------

    def available(self) -> bool:
        try:
            import openai  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def _key(self) -> str | None:
        return self._api_key_resolver()

    def _make_client(self):
        """Resolve the key and build a client, or raise the typed not-connected error.

        Used both by :meth:`_validate` (status/connect/models) and — via
        :meth:`open_chat`'s factory — by the session's worker thread, so a missing
        key surfaces the SAME :class:`AINotConnectedError` on either path.
        """
        key = self._key()
        if not key and (self._require_api_key or not self._base_url):
            detail = _NO_TRUSTED_KEY_DETAIL if self._require_api_key else _NO_KEY_DETAIL
            raise AINotConnectedError(detail)
        # A base_url with no key = a keyless endpoint (local vLLM/Ollama/LM Studio);
        # the SDK still needs a non-empty api_key, so pass a harmless placeholder.
        return build_client(
            key or _PLACEHOLDER_KEY,
            base_url=self._base_url,
            api_version=self._api_version,
            timeout=_CLIENT_TIMEOUT,
            follow_redirects=self._follow_redirects,
        )

    def make_client(self):
        """Public client factory for the trusted inspector sharing this profile."""
        return self._make_client()

    # -- status --------------------------------------------------------------

    def _unavailable_status(self) -> ProviderStatus:
        return ProviderStatus(self.name, available=False, connected=False, detail=_OPENAI_UNAVAILABLE)

    def _cheap_status(self) -> ProviderStatus:
        """Readiness WITHOUT a network call: is a key resolvable? (Key lookup is a
        cheap env/keyring read — unlike Copilot's CLI probe — so this is the common
        path and ``force`` upgrades it to a real /models validation.)"""
        if not self.available():
            return self._unavailable_status()
        if not self._key() and not self._base_url:
            return ProviderStatus(self.name, available=True, connected=False, detail=_NO_KEY_DETAIL)
        return ProviderStatus(
            self.name, available=True, connected=True, detail=self._configured_detail()
        )

    def _configured_detail(self) -> str:
        """A value-free status line: the endpoint host for a custom base_url, else
        just that a key is set (canonical OpenAI)."""
        if self._base_url:
            return f"Endpoint: {_host(self._base_url)}."
        return "API key configured."

    def status(self, force: bool = False) -> ProviderStatus:
        if not self.available():
            return self._unavailable_status()
        if not force:
            fresh = (
                self._cached_status is not None
                and (time.monotonic() - self._cached_at) < _STATUS_TTL
            )
            return self._cached_status if fresh else self._cheap_status()
        status = self._probe()
        self._cached_status = status
        self._cached_at = time.monotonic()
        return status

    def cached_status(self) -> ProviderStatus | None:
        """Last known status without a network call — the cheap key-present check
        (the hub auto-loads this so opening the hub never hits the API)."""
        if not self.available():
            return self._unavailable_status()
        if (
            self._cached_status is not None
            and (time.monotonic() - self._cached_at) < _STATUS_TTL
        ):
            return self._cached_status
        return self._cheap_status()

    def _probe(self) -> ProviderStatus:
        """Validate access with one cheap call (``models.list``)."""
        if not self._key() and not self._base_url:
            return ProviderStatus(self.name, available=True, connected=False, detail=_NO_KEY_DETAIL)
        try:
            client = self._make_client()
            next(iter(client.models.list()), None)  # one page is enough to prove access
        except AINotConnectedError:
            return ProviderStatus(self.name, available=True, connected=False, detail=_NO_KEY_DETAIL)
        except Exception as exc:  # noqa: BLE001 - report, never raise into a probe
            return ProviderStatus(
                self.name, available=True, connected=False, detail=friendly_error(str(exc))
            )
        detail = "Connected" + (f" to {_host(self._base_url)}" if self._base_url else "") + "."
        return ProviderStatus(self.name, available=True, connected=True, detail=detail)

    def connect(self, host: str | None = None) -> ProviderStatus:
        """OpenAI has no browser/device flow — validate the configured key and report.

        (``host`` is accepted for signature-compatibility with the hub's login route
        and the Copilot provider, but there is no GHE-style host to target.)
        """
        status = self._probe()
        self._cached_status = status
        self._cached_at = time.monotonic()
        return status

    def login_interactive(self, host: str | None = None) -> int:
        """No OAuth to drive: print how to configure the key and succeed."""
        print(_NO_KEY_DETAIL)
        return 0

    # -- models --------------------------------------------------------------

    def models_error(self) -> str:
        return self._models_error

    def list_models(self, force: bool = False) -> list[dict]:
        """Chat-capable models the key can use, as value-free dicts. ``[]`` if no key.

        Unlike Copilot's ``ModelInfo``, OpenAI's listing carries no reasoning-effort
        or premium-multiplier metadata (reasoning effort is a per-request param), and
        it includes non-chat models — so it is filtered to chat ids, ``multiplier``
        and ``default_effort`` are left empty, and ``efforts`` is synthesised locally
        (:func:`_efforts_for`) so a reasoning model's picker is actually shown.
        """
        if not self.available() or (not self._key() and not self._base_url):
            return []
        fresh = (
            self._cached_models is not None
            and (time.monotonic() - self._models_at) < _MODELS_TTL
        )
        if fresh and not force:
            return self._cached_models
        try:
            client = self._make_client()
            # Canonical OpenAI ids match a known chat prefix; a custom endpoint's
            # ids (llama/qwen/mistral/…) must NOT be prefix-filtered away.
            require_prefix = not self._base_url
            models = sorted(
                {m.id for m in client.models.list() if _is_chat_model(m.id, require_prefix)},
            )
            dicts = [
                {
                    "id": mid,
                    "name": mid,
                    "efforts": _efforts_for(mid),
                    "default_effort": "",
                    "multiplier": None,
                }
                for mid in models
            ]
            error = ""
        except Exception as exc:  # noqa: BLE001 - never raise into the hub
            dicts = []
            error = friendly_error(str(exc))
        self._cached_models = dicts
        self._models_at = time.monotonic()
        self._models_error = error
        return dicts

    # -- chat ----------------------------------------------------------------

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
        catalog=None,
        read_only: bool = False,
        run_investigation=None,
        pii=None,
        traceback_guard: bool = True,
        background: bool = False,
        allow_read_tools: bool = True,
        trusted_customer_data: bool = False,
        output_guard=None,
    ):
        """Open a long-lived, streaming, value-blind chat session (Chat Completions).

        Mirrors :meth:`mooring.ai.copilot.CopilotProvider.open_chat` — the SUPERSET
        the hub calls with (``traceback_guard`` + ``background`` beyond the base
        Protocol). The session is a :class:`~mooring.ai.openai_session.OpenAIChatSession`
        (a ``ChatBroadcaster``), so the PII/traceback guards, the send/confirm valve,
        and idle reaping are inherited unchanged. ``background=True`` returns the
        still-starting session immediately and streams a bad/missing key as a
        ``fail`` event (never raises), so the hub can offer an "add your API key"
        panel instead of a dead error.
        """
        if not self.available():
            raise AIError(_OPENAI_UNAVAILABLE)
        from mooring.ai import ner
        from mooring.ai.openai_session import OpenAIChatSession
        from mooring.ai_config import PiiConfig

        pii = pii or PiiConfig()
        # Resolve "auto" -> a concrete NER backend + model ONLY when the name pass
        # will actually run (guard on AND names on), matching the copilot path so a
        # default, guard-off install never imports spaCy just to open a chat.
        backend = pii.name_backend
        name_model = pii.name_model
        if pii.enabled and pii.names:
            backend = ner.resolve_backend(pii.name_backend)
            name_model = ner.model_for(backend, pii.name_model, pii.name_revision, pii.name_variant)

        # The factory runs on the session's worker thread (key lookup + client build
        # off the open path). A missing key raises AINotConnectedError there, which
        # the session turns into a not_connected "fail" event under background=True.
        def client_factory():
            return self._make_client()

        # store=False is OpenAI's own retention control and only canonical OpenAI
        # honours it; a strict OpenAI-compatible server may reject the unknown field,
        # so send it only when talking to OpenAI itself (no custom base_url).
        store = False if not self._base_url else None

        session = OpenAIChatSession(
            model=(model or "").strip() or self.model,
            reasoning_effort=reasoning_effort,
            system_context=system_context,
            workspace=workspace,
            folders=folders,
            notebook_rel=notebook_rel,
            dictionary=dictionary,
            semantic_models=semantic_models,
            helpers=helpers,
            catalog=catalog,
            read_only=read_only,
            run_investigation=run_investigation,
            pii_enabled=pii.enabled,
            pii_block=pii.block_prompt,
            pii_names=pii.enabled and pii.names,
            pii_name_labels=pii.name_labels,
            pii_name_threshold=pii.name_threshold,
            pii_name_model=name_model,
            pii_name_backend=backend,
            traceback_guard=traceback_guard,
            client_factory=client_factory,
            store=store,
            allow_read_tools=allow_read_tools,
            trusted_customer_data=trusted_customer_data,
            output_guard=output_guard,
        )
        session.start(block=not background)
        return session


def _is_chat_model(model_id: str, require_prefix: bool = True) -> bool:
    """Whether ``model_id`` looks like a chat model.

    For canonical OpenAI (``require_prefix``) an id must match a known chat-model
    prefix. For a custom ``base_url`` (a gateway, aggregator, or local server) keep
    everything that isn't obviously a non-chat model (embeddings / tts / whisper /
    …), so llama / qwen / mistral / deepseek / etc. are not hidden."""
    low = (model_id or "").lower()
    if any(marker in low for marker in _NON_CHAT_MARKERS):
        return False
    if require_prefix and not low.startswith(_CHAT_PREFIXES):
        return False
    return True


def _efforts_for(model_id: str) -> list[str]:
    """Reasoning-effort choices to advertise for ``model_id``; ``[]`` hides the picker.

    "Is this a reasoning model?" is the SAME advisory heuristic the request path gates
    its ``reasoning_effort`` param on: imported from ``openai_session`` (function-local,
    mirroring that module's own import of :func:`friendly_error`) so the prefixes live
    in ONE place and the picker can never offer an effort the request would drop. A
    non-reasoning id (``gpt-4o``, ``llama-3-70b``) gets ``[]`` and stays hidden.
    """
    from mooring.ai.openai_session import _is_reasoning_model

    return list(_REASONING_EFFORTS) if _is_reasoning_model(model_id) else []


def _host(base_url: str) -> str:
    """The host[:port] of a base URL — computed without a urllib import (the ai/
    layer may not import urllib per the marimo-internals-isolated contract)."""
    tail = base_url.split("://", 1)[-1]
    return tail.split("/", 1)[0] or base_url
