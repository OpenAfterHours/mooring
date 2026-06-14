"""The AI-provider seam.

A provider is the thing the hub's "AI helper" calls to turn a dataset schema +
a goal into code. The contract every provider must honour: ``generate`` is sent
**only** the schema text and the instruction it is given — never any data
values. (The schema text is built by :mod:`mooring.schema`, which emits names
and dtypes only.)

GitHub Copilot is the only provider implemented today. ``get_provider`` lazily
imports the concrete backend so that importing this package never drags in the
Copilot SDK (and its bundled CLI) until a generation actually happens.
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

    def generate(self, *, schema_context: str, instruction: str, target: str = "polars") -> str:
        """Return generated code. Sends ONLY ``schema_context`` + ``instruction``."""
        ...


def get_provider(app_cfg: "AppConfig") -> AIProvider:
    """Build the configured provider. Import of the backend is deferred."""
    name = (app_cfg.ai_provider or "copilot").strip().lower()
    if name == "copilot":
        from mooring.ai.copilot import CopilotProvider

        return CopilotProvider(model=app_cfg.ai_model)
    raise AIError(f"Unknown AI provider {name!r}. Known: copilot.")
