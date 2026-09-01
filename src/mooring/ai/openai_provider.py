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
key must not travel with the repo). ``base_url`` / ``api_version`` / ``timeout_sec``
are the only config knobs, and they are value-free: they point the client at an
OpenAI-compatible gateway or an Azure resource so an enterprise can keep data in its
own tenant, and say how long to wait for it.
"""

from __future__ import annotations

import importlib
import math
import os
import re
import time
from collections.abc import Callable, Mapping

from mooring.ai.base import AIError, AINotConnectedError, ProviderStatus
from mooring.ai_config import OPENAI_TIMEOUT_DEFAULT

_STATUS_TTL = 45.0  # cache a (possibly network-validating) status probe this long
_MODELS_TTL = 300.0  # cache the model list this long
# HTTP timeouts are SPLIT rather than one flat number, because the two legs guard
# different failures and want wildly different budgets. A bare float handed to the
# SDK is expanded by httpx into Timeout(connect=N, read=N, write=N, pool=N), so the
# old flat 30.0 set both — and the read leg was the one that mattered.
#
# CONNECT is the real "a hung gateway can't wedge us" guard the flat constant was
# reaching for. Ten seconds is generous for a TCP+TLS handshake to any endpoint that
# is actually up, and nothing about a model or a prompt changes it — so it stays a
# constant, deliberately not a knob.
_CONNECT_TIMEOUT = 10.0
# READ/WRITE/POOL is the budget for the model itself, and it is configurable
# (``ai.openai_timeout_sec`` / ``MOORING_AI_OPENAI_TIMEOUT_SEC``, default
# :data:`~mooring.ai_config.OPENAI_TIMEOUT_DEFAULT`). On a STREAMING request the read
# timeout is the maximum gap allowed BETWEEN chunks — INCLUDING the gap before the
# first one — so it is really "how long the model may think in silence". A gateway
# that buffers the SSE response instead of passing it through (nginx without
# `proxy_buffering off`, Cloudflare, Azure APIM, a non-streaming translation shim)
# sends nothing at all until the upstream completion finishes, so a reasoning model
# thinking for more than the budget trips it every single time. The OpenAI SDK's own
# considered default here is 600s, chosen precisely because reasoning models go
# quiet for minutes; the flat 30.0 silently cut that 20x.
#
# Why mooring defaults to 300 and not the SDK's 600: mooring consumes the stream on a
# worker thread whose cancel flag is only checked inside the ``for chunk in stream``
# loop, so a stalled read cannot be interrupted by the user's Stop button. An
# unbounded stall therefore wedges a chat turn for mooring in a way it would not for
# a plain SDK user. 300s covers any realistic reasoning turn and halves the
# worst-case wedge; the knob covers anyone who needs longer.
#
# The SDK retries a timeout by default (max_retries=2 → three attempts). On a
# streaming completion that has already made the upstream model generate, each retry
# re-bills a full reasoning generation and multiplies the wall-clock before the user
# sees anything. One retry still absorbs a transient connect blip or a 429, and caps
# the blast radius at two paid attempts instead of three.
_MAX_RETRIES = 1
# The floor under the configurable budget. Below a second nothing can succeed — TLS
# alone rarely finishes that fast — so a sub-second value is a typo, not a choice, and
# it is treated exactly like a 0: fall back to the packaged default. It matches the
# loader's own floor (``_as_positive_int`` refuses anything under 1).
_MIN_TIMEOUT = 1.0
# METADATA calls (``models.list``, behind status(force=True) and list_models) get their
# OWN short budget and must never inherit the chat one. Nothing is THINKING on the
# other end of a model listing — it is a lookup — so a wait past half a minute means a
# hung endpoint, not a slow answer, and the knob's promise is "how long the MODEL may
# think", not "how long a lookup may hang".
#
# This is not cosmetic. The hub's "Check" button calls status(force=True) AND
# list_models(force=True) back to back in ONE sync route (hub/routes/chat.py), and the
# Health check does the same shape (hub/routes/setup.py) — both on a threadpool worker
# with nothing to cancel them. Against a gateway that accepts the connection and then
# sends nothing (the exact audience for a configurable timeout), inheriting the chat
# budget would block that worker for ~10 minutes at the 300s default and hours at the
# 3600 ceiling. ``copilot._PROBE_TIMEOUT`` is 30s for precisely this reason.
_PROBE_TIMEOUT = 30.0
# Retries go the OTHER way from the chat client's, for the same underlying reason: a
# metadata call is cheap, idempotent, and nothing generated, so a second retry buys
# resilience for free — whereas a streaming completion's retry re-bills a whole
# reasoning generation. Fast AND resilient, because the budget is short.
_PROBE_MAX_RETRIES = 2
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
# HTTP statuses that ARE a timeout: 408 request timeout, 504 gateway timeout, and
# Cloudflare's 524 ("a timeout occurred") — the one a Cloudflare-fronted gateway
# returns when the upstream took longer than its own fixed budget.
_TIMEOUT_CODES = ("408", "504", "524")
# Text markers for a gateway that reports a timeout with no SDK status code. Each is
# a PHRASE, never the bare word "timeout": an error body that merely echoes a
# parameter named `timeout` is not a report that one elapsed.
_TIMEOUT_MARKERS = (
    "timed out",
    "time-out",  # nginx's own wording: "504 Gateway Time-out"
    "gateway timeout",
    "read timeout",
    "request timeout",
    "connection timeout",
    "deadline exceeded",  # gRPC-shaped gateways
)
# A bare status number in a code-less body ("HTTP 524", "504 Gateway Time-out").
#
# It is ANCHORED, not merely word-bounded, because a word boundary is no protection at
# all here: "you requested 504 tokens more than the 8192 limit", "you requested 408
# tokens", and "model qwen-524 is not available" all satisfy \b(?:408|504|524)\b and
# would every one of them have been reported as a gateway timeout. A status code is
# only a status code where a status code goes — at the very start of the message, or
# behind an "HTTP"/"HTTP/1.1" or "status:" introducer. Anywhere else a three-digit run
# is a count, an id, or a model name.
_BARE_TIMEOUT_STATUS = re.compile(
    r"(?:^\s*"  # "504 Gateway Time-out"
    r"|\bhttp/?[\d.]*\s+"  # "HTTP 524", "HTTP/1.1 504 Gateway Timeout"
    r"|\bstatus(?:\s+code)?\s*[:=]?\s*"  # "status: 504", "status code 408"
    r")(?:408|504|524)\b"
)
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
_TIMEOUT_HELP = (
    "The AI endpoint timed out before a reply arrived. A reasoning model sends nothing "
    "at all while it thinks, and a gateway that BUFFERS the streamed response instead of "
    "passing chunks straight through (nginx without `proxy_buffering off`, Cloudflare, "
    "Azure APIM, a non-streaming shim) holds the whole reply back until the model has "
    "finished — so mooring sees a silent connection, not a slow one. Raise Settings -> "
    "'AI request timeout' (config ai.openai_timeout_sec, env MOORING_AI_OPENAI_TIMEOUT_SEC; "
    f"default {OPENAI_TIMEOUT_DEFAULT}s), or turn response buffering off on the gateway so "
    "chunks arrive as the model produces them."
)


def _timeout_types() -> tuple[type[BaseException], ...]:
    """Timeout exception classes from the optional deps, resolved lazily.

    Both imports are optional on purpose. ``openai`` (and ``httpx``, which ships only
    as ITS dependency) come with the ``mooring[openai]`` extra, and this module must
    stay importable without either — :meth:`OpenAIProvider.available` catches that
    ImportError deliberately. An absent package simply contributes no class; the
    builtin ``TimeoutError`` (what ``socket.timeout`` aliases) is always in the tuple,
    so the check degrades rather than breaking.
    """
    found: list[object] = [TimeoutError]
    try:
        import openai

        found.append(openai.APITimeoutError)
    except Exception:  # noqa: BLE001 - absent/older SDK contributes nothing
        pass
    # BOTH http libraries, deliberately. The SDK's own choice MOVED — openai <= 2.x is
    # built on httpx, openai 3.x on httpx2 — and httpx2.TimeoutException is NOT a
    # subclass of httpx.TimeoutException, so checking one library would silently miss
    # every timeout raised by the other. It matters most in exactly the case this whole
    # change is about: a stall mid-stream raises the RAW transport exception (the SDK's
    # APITimeoutError wrapping happens around the request, not around chunk reads).
    try:
        import httpx

        found.append(httpx.TimeoutException)
    except Exception:  # noqa: BLE001 - ships only as the openai extra's own dependency
        pass
    try:
        import httpx2

        found.append(httpx2.TimeoutException)
    except Exception:  # noqa: BLE001 - likewise, and only for openai 3.x
        pass
    return tuple(t for t in found if isinstance(t, type) and issubclass(t, BaseException))


def _is_timeout_exc(exc: BaseException | None, *, walk: bool = True) -> bool:
    """Whether ``exc`` — or, with ``walk``, something it wraps — is a transport timeout.

    The exception TYPE is stronger evidence than any string, and here it is often the
    ONLY evidence: a mid-stream ``httpx.ReadTimeout`` routinely stringifies to an
    empty or near-empty message, which would leave the text branches nothing to match
    and hand the user a bare "OpenAI request failed:" with nothing after the colon.

    ``walk`` exists because the two halves of that evidence are NOT equally strong,
    and :func:`friendly_error` consults them at different points. ``exc`` itself being
    a timeout class is decisive. Something merely reachable from it is not: Python
    sets ``__context__`` on *any* exception raised inside an ``except`` block, so an
    unrelated re-raise after a swallowed retry — ``except ReadTimeout: raise
    RuntimeError("Error code: 401 ...")`` — leaves a timeout hanging off a message
    that plainly says otherwise. Walked evidence is therefore consulted LAST, where it
    can only rescue a message that yielded no verdict, never overrule one.
    """
    if exc is None:
        return False
    types_ = _timeout_types()
    if isinstance(exc, types_):
        return True
    if not walk:
        return False
    for _ in range(6):  # bounded: a cause chain can be cyclic-ish under re-raise
        nxt = exc.__cause__ or exc.__context__
        if nxt is None or nxt is exc:
            return False
        exc = nxt
        if isinstance(exc, types_):
            return True
    return False


def _status_code(low: str) -> str:
    """The HTTP status the SDK prefixes its message with ("Error code: 401 - ..."),
    or "" when the message carries none (a gateway may raise a bare error)."""
    match = re.search(r"error code:\s*(\d{3})", low)
    return match.group(1) if match else ""


def _has(low: str, markers: tuple[str, ...]) -> bool:
    return any(marker in low for marker in markers)


def friendly_error(msg: str, exc: BaseException | None = None) -> str:
    """Map a raw SDK/gateway error onto one short, actionable line.

    ``exc``, when the caller has the exception object, is used for ONE thing: a
    timeout. It is optional and every existing single-argument call keeps its exact
    behaviour. It enters at TWO points, and the split matters — ``exc`` being a
    timeout class outranks the text (that failure's message is routinely empty);
    a timeout merely reachable through ``__cause__``/``__context__`` outranks
    nothing, and is read last (see :func:`_is_timeout_exc`).

    Otherwise order is load-bearing. The STATUS CODE decides first where the SDK
    gives one, because an error body echoes the request's parameters — a 401 whose
    body lists the request keys, or a 429 reading "Rate limit reached for gpt-5
    (params: reasoning_effort=high)", must be reported as auth and rate-limit faults,
    not as a rejected effort. That is also why a CODED timeout outranks the effort
    branch while a code-less one does not: with no status, "the body is only echoing
    parameters" is an assumption, not an observation. The effort branch needs BOTH
    the parameter name and a rejection word; the substring auth/rate/timeout checks
    below it still catch a code-less message, and the original text is always
    preserved.
    """
    # The exception TYPE decides before any text, and ONLY when ``exc`` IS a timeout
    # class: that is the one failure whose message is routinely empty (see
    # :func:`_is_timeout_exc`), so there is nothing for the text branches to read.
    # Note ``walk=False`` — a timeout merely reachable through __cause__/__context__
    # is weak evidence and is consulted at the BOTTOM instead, so it can never
    # overrule a message that states its own verdict.
    if _is_timeout_exc(exc, walk=False):
        return _TIMEOUT_HELP
    low = msg.lower()
    code = _status_code(low)
    if code == "401" or (not code and _has(low, _AUTH_MARKERS)):
        return _AUTH_HELP
    if code == "429" or (not code and _has(low, _RATE_MARKERS)):
        return _RATE_HELP
    # A CODED timeout belongs here, above the effort branch, for exactly the reason
    # the status branches are ordered this way: a gateway timeout's body echoes the
    # request's parameters too, so an effort branch reading that echo would report
    # "your reasoning_effort was rejected" for a request the model never answered. It
    # sits BELOW 401/429 because those are more specific claims about the same
    # response and a body can carry both (a 429 whose text says "timed out waiting for
    # capacity" is a rate limit, not a transport timeout).
    #
    # The CODE-LESS arm is deliberately NOT here. That justification — "a 504's body
    # echoes parameters" — only holds where a status was actually given. With no code,
    # a body that both names reasoning_effort and says it was refused is an effort
    # rejection whatever else it mentions (LiteLLM and vLLM do emit code-less bodies),
    # so the phrase/bare-status reading runs AFTER the effort branch, below.
    if code in _TIMEOUT_CODES:
        return _TIMEOUT_HELP
    if _has(low, _EFFORT_FIELDS) and _has(low, _EFFORT_REJECTIONS):
        return f"{_EFFORT_HELP} (OpenAI said: {msg})"
    # A status the branches above don't claim (a 400, a 5xx), so fall back to the
    # substring reading of the body.
    if _has(low, _AUTH_MARKERS):
        return _AUTH_HELP
    if _has(low, _RATE_MARKERS):
        return _RATE_HELP
    # The bare-status match is consulted ONLY when the SDK gave no code: a code WAS
    # given and it was not a timeout one, so a stray "504" in that text is not the
    # verdict. The phrase markers still apply either way — a 500 whose body says
    # "upstream timed out" is a timeout the SDK simply did not classify.
    if _has(low, _TIMEOUT_MARKERS) or (not code and _BARE_TIMEOUT_STATUS.search(low)):
        return _TIMEOUT_HELP
    # Last: a timeout reachable only through the cause chain. Nothing above claimed the
    # message, so there is no verdict left to steal — this rescues the wrapped
    # empty-message stall and nothing else.
    if _is_timeout_exc(exc):
        return _TIMEOUT_HELP
    return f"OpenAI request failed: {msg}"


def timeout_seconds(value: object) -> float:
    """Coerce a configured read/write/pool budget to a usable positive float.

    Defence in depth beside :func:`mooring.ai_config.load_ai_config`, which already
    clamps the knob it loads: this module is also reachable from an eval harness and
    from any caller passing its own number, and a 0 / negative / non-numeric /
    infinite budget would build a client that fails every request instantly (or never
    gives up at all) rather than one that waits. Anything unusable falls back to the
    packaged default rather than raising — a bad number is a misconfiguration, not a
    reason for the chat to be unopenable — and that promise is kept for EVERY input,
    including the three that used to slip through:

    * ``float(10**400)`` raises ``OverflowError``, which is neither a TypeError nor a
      ValueError. A knob documented as never raising must not be the reason the
      provider cannot be constructed, so the except is broad.
    * ``True`` is an ``int`` subclass, so it floats to ``1.0`` — a one-second budget
      that would fail every reasoning turn. A bool is never a number of seconds; it is
      rejected explicitly, the same way ``hub.settings_schema.coerce`` rejects one.
    * ``0.001`` / ``1e-9`` are finite and positive, so nothing caught them. Below
      :data:`_MIN_TIMEOUT` a budget cannot be met by any real endpoint, so it is
      treated exactly like a 0.
    """
    if isinstance(value, bool):
        return float(OPENAI_TIMEOUT_DEFAULT)
    try:
        num = float(value)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - OverflowError, __float__ raising, anything
        return float(OPENAI_TIMEOUT_DEFAULT)
    if not math.isfinite(num) or num < _MIN_TIMEOUT:
        return float(OPENAI_TIMEOUT_DEFAULT)
    return num


def _timeout_object(seconds: float):
    """A SPLIT timeout for the SDK: ``seconds`` on read/write/pool, a short connect leg.

    The Timeout class is taken from the SDK's OWN http library rather than by importing
    httpx directly, because that library MOVED: openai <= 2.x is built on httpx, openai
    3.x on httpx2, and their Timeout classes are unrelated. Handing an ``httpx.Timeout``
    to an httpx2-backed client does not raise — it nests one object inside the other and
    yields a client whose four legs are all garbage. ``openai.Timeout`` is the right
    class on every version the extra allows, by construction.
    """
    import openai

    timeout_cls = getattr(openai, "Timeout", None)
    if timeout_cls is None:
        # No re-export. Take the class off whichever http library is actually present —
        # and try BOTH, because the one that matters is the one that moved: openai 3.x
        # pulls httpx2 and NOT httpx, so a fallback that only knew httpx would raise
        # ImportError on exactly the newer SDK it would be reached on.
        for module_name in ("httpx", "httpx2"):
            try:
                timeout_cls = importlib.import_module(module_name).Timeout
                break
            except Exception:  # noqa: BLE001 - absent library contributes nothing
                continue
    if timeout_cls is None:  # pragma: no cover - exercised with both libs masked
        # Neither library importable. Degrade to a bare float rather than failing to
        # build a client at all: it re-applies one number to all four legs (the very
        # shape of the original bug), but the leg that actually matters — read — still
        # gets the right budget, and a long connect leg is a far smaller fault than an
        # unusable provider.
        return float(seconds)
    return timeout_cls(float(seconds), connect=_CONNECT_TIMEOUT)


def build_client(
    api_key: str,
    *,
    base_url: str = "",
    api_version: str = "",
    timeout: float,
    max_retries: int = _MAX_RETRIES,
    follow_redirects: bool = True,
):
    """Construct the sync OpenAI client for the resolved key + endpoint.

    ``api_version`` set → a classic Azure deployment (``AzureOpenAI`` with
    ``azure_endpoint``); otherwise the standard client, with ``base_url`` pointing
    at an OpenAI-compatible gateway / Azure v1 endpoint when given.

    ``timeout`` is the read/write/pool budget in SECONDS — how long the model may go
    quiet. The connect leg is pinned separately and short (:data:`_CONNECT_TIMEOUT`);
    passing a bare float to the SDK instead would apply ONE number to both legs, which
    is the bug this split exists to prevent.

    ``max_retries`` defaults to the CHAT cap (:data:`_MAX_RETRIES`, one) because a
    retried streaming completion re-bills a whole generation. A metadata call passes
    its own — see :meth:`OpenAIProvider._metadata_client`.
    """
    # Lazy and local, so this module stays importable when the mooring[openai] extra is
    # absent (``available()`` catches that ImportError).
    import openai

    client_timeout = _timeout_object(timeout_seconds(timeout))
    http_client = None
    if not follow_redirects:
        # A trust profile pins one HTTPS host. Following a gateway redirect to a
        # different host would silently widen that approval.
        http_client = openai.DefaultHttpxClient(follow_redirects=False, timeout=client_timeout)
    if api_version:
        kwargs = {
            "api_key": api_key,
            "azure_endpoint": base_url or None,
            "api_version": api_version,
            "timeout": client_timeout,
            "max_retries": int(max_retries),
        }
        if http_client is not None:
            kwargs["http_client"] = http_client
        return openai.AzureOpenAI(**kwargs)
    kwargs = {"api_key": api_key, "timeout": client_timeout, "max_retries": int(max_retries)}
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
        timeout: float | int | None = None,
        name: str = "openai",
    ) -> None:
        self.model = (model or "").strip()
        self._base_url = (base_url or "").strip()
        self._api_version = (api_version or "").strip()
        # Seconds the MODEL may go quiet, from ``ai.openai_timeout_sec`` — the chat
        # budget only; the metadata probes keep their own short one
        # (:meth:`_metadata_client`). Every construction site passes it
        # (mooring.ai.base.get_provider and the hub's trusted route); None means "the
        # packaged default", so a caller that does not care — a test, an eval harness
        # — still gets a sane client rather than a 30-second one.
        self._timeout = timeout_seconds(OPENAI_TIMEOUT_DEFAULT if timeout is None else timeout)
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

    def _make_client(self, *, timeout: float | None = None, max_retries: int | None = None):
        """Resolve the key and build a client, or raise the typed not-connected error.

        Used both by the probes (status/connect/models) and — via :meth:`open_chat`'s
        factory — by the session's worker thread, so a missing key surfaces the SAME
        :class:`AINotConnectedError` on either path.

        ``timeout`` / ``max_retries`` default to the CHAT budget. They are overridden
        only by :meth:`_metadata_client`, and deliberately as explicit arguments rather
        than by a second construction site: the key resolution, the placeholder-key
        rule, the endpoint and the redirect pin must stay identical on both paths, and
        the only thing a metadata call changes is how long it is willing to wait.
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
            timeout=self._timeout if timeout is None else timeout,
            max_retries=_MAX_RETRIES if max_retries is None else max_retries,
            follow_redirects=self._follow_redirects,
        )

    def _metadata_client(self):
        """A client for the value-free METADATA calls, on the short probe budget.

        ``models.list`` is a lookup, not a generation: nothing on the other end is
        thinking, so it has no business inheriting the chat budget (up to an hour) or
        the chat retry cap. See :data:`_PROBE_TIMEOUT` for why that inheritance is a
        real hazard rather than a tidiness point.
        """
        return self._make_client(timeout=_PROBE_TIMEOUT, max_retries=_PROBE_MAX_RETRIES)

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
        """Validate access with one cheap call (``models.list``), on the probe budget.

        This runs on a hub threadpool worker with nothing able to cancel it (the
        "Check" button and the Health check both force it), so it must stay bounded by
        :data:`_PROBE_TIMEOUT` regardless of how high the chat budget is set.
        """
        if not self._key() and not self._base_url:
            return ProviderStatus(self.name, available=True, connected=False, detail=_NO_KEY_DETAIL)
        try:
            client = self._metadata_client()
            next(iter(client.models.list()), None)  # one page is enough to prove access
        except AINotConnectedError:
            return ProviderStatus(self.name, available=True, connected=False, detail=_NO_KEY_DETAIL)
        except Exception as exc:  # noqa: BLE001 - report, never raise into a probe
            # The exception, not just its text: configuring a gateway is where an
            # admin meets a timeout FIRST, and that is the failure whose message is
            # routinely empty.
            return ProviderStatus(
                self.name, available=True, connected=False, detail=friendly_error(str(exc), exc)
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
            # The probe budget, not the chat one: a listing is a lookup. The hub's
            # "Check" runs this straight after status(force=True) in the SAME sync
            # route, so the two waits ADD up on one blocked worker.
            client = self._metadata_client()
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
            error = friendly_error(str(exc), exc)  # the exception too — see _probe
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
        applier=None,
        max_tool_iters: int | None = None,
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
            # Edit mode (None = propose mode) and the runaway ceiling for THIS backend's
            # own tool loop — the two the copilot path splits on, because only this one
            # drives the loop itself.
            applier=applier,
            max_tool_iters=max_tool_iters,
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
