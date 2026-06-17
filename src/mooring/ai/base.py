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
    from mooring.config import AppConfig


class AIError(Exception):
    """A provider-level failure, surfaced verbatim to the hub UI."""


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
        pii_enabled: bool = False,
        pii_block: bool = True,
        pii_names: bool = False,
        pii_name_labels: tuple[str, ...] | None = None,
        pii_name_threshold: float = 0.7,
        pii_name_model: str | None = None,
    ):
        """Open a long-lived, streaming chat session (a ``ChatBroadcaster``).

        Sends the model ONLY ``system_context`` (schema + notebook source, plus
        any opt-in team context already folded in) and the analyst's turns.
        ``dictionary`` (a parsed index) enables the value-free dictionary tools.
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
    raise AIError(f"Unknown AI provider {name!r}. Known: copilot.")
