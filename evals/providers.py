"""Opening the session a case runs against.

A real-model run goes through mooring's own provider seam
(:func:`mooring.ai.base.get_provider` → ``open_chat``) and nothing else. There is
deliberately no HTTP client in this package: an eval that spoke to the API itself
would measure a request mooring does not send — a different system prompt, a
different tool schema, no egress guard — and its capability card would be about
some other product.
"""

from __future__ import annotations

from dataclasses import replace

from mooring.ai.base import get_provider
from mooring.config import AppConfig


def provider_config(
    app_cfg: AppConfig,
    *,
    provider: str,
    model: str = "",
    base_url: str = "",
    api_version: str = "",
) -> AppConfig:
    """``app_cfg`` with the provider settings the command line asked for.

    Built by replacing fields on the case's own in-memory config rather than by
    loading one, so a sweep never inherits the operator's configured
    provider/model — the whole point of the eval is that the model under test is
    the one that was named.
    """
    return replace(
        app_cfg,
        ai=replace(
            app_cfg.ai,
            provider=provider,
            model=model,
            openai_base_url=base_url or app_cfg.ai.openai_base_url,
            openai_api_version=api_version or app_cfg.ai.openai_api_version,
        ),
    )


def preflight(*, provider: str, model: str = "", base_url: str = "", api_version: str = "") -> str:
    """``""`` if a sweep can run, else why it cannot.

    Checked ONCE before the sweep. Without it a missing extra or an unset API key
    reads as thirty-one failed cases — a capability card saying the model cannot do
    anything, when the truth is that it was never asked.
    """
    cfg = provider_config(
        AppConfig(),
        provider=provider,
        model=model,
        base_url=base_url,
        api_version=api_version,
    )
    try:
        backend = get_provider(cfg)
    except Exception as exc:  # noqa: BLE001  # an unknown provider name, mostly
        return str(exc)
    if not backend.available():
        # Both providers ship as an optional extra named after themselves.
        return (
            f"the {provider} provider is not usable on this machine "
            f"(install it with `uv sync --extra {provider}`)."
        )
    try:
        status = backend.status(force=True)
    except Exception as exc:  # noqa: BLE001  # a network/keyring failure is a reason, not a crash
        return f"could not check {provider} status: {exc}"
    if not status.connected:
        return f"not signed in to {provider}: {status.detail or 'no credentials found'}"
    return ""


def real_opener(
    *,
    provider: str,
    model: str = "",
    reasoning_effort: str = "",
    base_url: str = "",
    api_version: str = "",
):
    """A :data:`~evals.harness.SessionOpener` that opens a real provider session."""

    def opener(
        *,
        case,
        app_cfg: AppConfig,
        system_context: str,
        workspace,
        folders,
        notebook_rel: str,
        dictionary=None,
        semantic_models=None,
        helpers=None,
        catalog=None,
        **_ignored,
    ):
        cfg = provider_config(
            app_cfg,
            provider=provider,
            model=model,
            base_url=base_url,
            api_version=api_version,
        )
        session = get_provider(cfg).open_chat(
            system_context=system_context,
            workspace=workspace,
            folders=folders,
            notebook_rel=notebook_rel,
            model=model or None,
            reasoning_effort=reasoning_effort or None,
            dictionary=dictionary,
            semantic_models=semantic_models,
            helpers=helpers,
            catalog=catalog,
        )
        return session

    return opener
