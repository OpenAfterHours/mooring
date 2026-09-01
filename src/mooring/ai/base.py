"""The AI-provider seam.

A provider is the thing the hub's "AI helper" calls to open a streaming chat over
a dataset schema + the analyst's goal. The contract every provider must honour:
it is sent **only** the value-blind system context (schema names + dtypes, the
notebook source, any opt-in team context) and the analyst's turns — never a data
value. (The schema text is built by :mod:`mooring.schema`, which emits names and
dtypes only.)

GitHub Copilot is the only provider implemented today. ``get_provider`` lazily
imports the concrete backend so that importing this package never drags in the
Copilot SDK (and its bundled CLI) until a chat actually opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mooring.ai_config import PiiConfig
    from mooring.config import AppConfig


class AIError(Exception):
    """A provider-level failure, surfaced verbatim to the hub UI."""


class AINotConnectedError(AIError):
    """The provider is installed/available but the user is not signed in.

    A *typed* sign-in failure (vs a generic AIError) so the session/UI can tell
    "you need to sign in to Copilot" apart from any other startup error and offer
    an in-app sign-in button instead of a dead error string. Copilot's sign-in is
    independent of mooring's GitHub login — it can even be a different account."""


@dataclass(frozen=True)
class ProviderStatus:
    """What the UI shows about a provider."""

    provider: str
    available: bool  # the backend is installed/usable on this machine
    connected: bool  # the user is signed in and ready to generate
    account: str = ""  # the signed-in identity, when known
    detail: str = ""  # a human-readable status line / next step


@runtime_checkable
class AIProvider(Protocol):
    name: str

    def available(self) -> bool:
        """Whether the backend is installed/usable on this machine (cheap)."""
        ...

    def status(self, force: bool = False) -> ProviderStatus:
        """Readiness/sign-in status. ``force`` re-checks instead of using a cache."""
        ...

    def connect(self) -> ProviderStatus:
        """Best-effort: drive/await sign-in. Raises :class:`AIError` on failure."""
        ...

    def login_interactive(self, host: str | None = None) -> int:
        """Drive an interactive sign-in to completion; return the CLI exit code."""
        ...

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
        pii: "PiiConfig | None" = None,
        allow_read_tools: bool = True,
        trusted_customer_data: bool = False,
        output_guard=None,
    ):
        """Open a long-lived, streaming chat session (a ``ChatBroadcaster``).

        Sends the model ONLY ``system_context`` (schema + notebook source, plus
        any opt-in team context already folded in) and the analyst's turns.
        ``dictionary`` (a parsed index) enables the value-free dictionary tools;
        ``semantic_models`` (pre-parsed :class:`mooring.pbip_model.SemanticModel`
        objects, already gated by config + the synced per-model opt-out) enables
        the Power BI model tools; ``catalog`` (a parsed
        :class:`mooring.ai.notebookindex.Catalog`, already gated by config and
        stripped of the team's AI-disabled notebooks) enables the repo-wide
        notebook-catalog tools. ``pii`` is the whole
        :class:`~mooring.ai_config.PiiConfig`, passed as one object so a guard
        field can't be silently dropped in transit; None disables the guard.
        ``read_only`` builds the session with NO propose/edit tool (an investigate
        sub-agent); ``run_investigation`` (a value-free coordinator closure) adds the
        ``mooring_investigate`` fan-out tool — never both at once (a read-only session
        is forced to drop ``run_investigation`` so an investigation cannot recurse).

        ``applier`` is the ONE switch between the write tool's two modes. ``None`` (the
        shipped default, and what ``[ai] auto_apply = false`` passes) leaves it in
        PROPOSE mode: the model emits a card and the analyst clicks Apply. An injected
        ``apply_edit(op_dicts, rationale)`` — :func:`mooring.app.auto_apply.make_applier`
        — puts the write inside the tool call and hands its value-free observation back
        as the tool result. Passed here rather than read from config INSIDE the session
        so ``ai/`` never has to reach up to ``app/`` for it. ``max_tool_iters`` is the
        per-turn tool-call RUNAWAY ceiling (``[ai] max_tool_iters``, policy-folded), and
        every backend honours it: one that drives its own tool loop spends it there,
        and one whose SDK owns the loop (Copilot) spends it at the tool boundary, which
        is the only part of that loop mooring owns.

        Raises :class:`AIError` if unavailable/not signed in.
        """
        ...

    def list_models(self, force: bool = False) -> list[dict]:
        """Available models as value-free dicts (id/name/efforts/...). [] if unavailable."""
        ...


def get_provider(app_cfg: "AppConfig") -> AIProvider:
    """Build the configured provider. Import of the backend is deferred."""
    name = (app_cfg.ai_provider or "copilot").strip().lower()
    if name == "copilot":
        from mooring.ai.copilot import CopilotProvider

        return CopilotProvider(model=app_cfg.ai_model)
    if name == "openai":
        from mooring.ai.openai_provider import OpenAIProvider

        return OpenAIProvider(
            model=app_cfg.ai_model,
            base_url=app_cfg.ai.openai_base_url,
            api_version=app_cfg.ai.openai_api_version,
            # Threaded here AND at the hub's trusted-route construction site
            # (hub/server.py::_trusted_provider_for). A timeout honoured on only one
            # of the two paths is exactly the bug the knob exists to fix.
            timeout=app_cfg.ai.openai_timeout_sec,
        )
    raise AIError(f"Unknown AI provider {name!r}. Known: copilot, openai.")
