"""The investigate application service — the value-free ``run_investigation`` closure.

Unlike :mod:`mooring.app.batch_service`, this is NOT a registry: an investigation runs
SYNCHRONOUSLY inside the copilot's ``mooring_investigate`` tool call (the parent turn
blocks on its tool result), so there is no run to register, no SSE to stream, and no
review tray. :func:`make_run_investigation` wires the pure
:class:`mooring.ai.investigate.InvestigatePlanner` to the hub-supplied context builder and
read-only session opener, and returns the closure the tool calls — or ``None`` when the
feature is off, so the hub passes ``None`` to the tool builder and ``mooring_investigate``
is never registered.

The injected callables originate ABOVE ``ai/`` (they need the provider + config), which is
exactly why the planner takes them by injection — this keeps both the planner and the
tool layer free of hub imports.
"""

from __future__ import annotations

from typing import Callable


def make_run_investigation(
    *,
    app_cfg,
    notebook_rel: str,
    build_context: Callable,
    open_readonly_session: Callable,
) -> Callable[[list], str] | None:
    """Build the ``run_investigation(branches) -> merged_findings`` closure, or ``None``
    when ``[ai.investigate] enabled`` is off.

    ``build_context(notebook_rel, dataset_rel) -> ctx`` and
    ``open_readonly_session(ctx, notebook_rel, model, effort) -> ChatBroadcaster`` are the
    hub's wiring (the read-only opener builds a session with NO propose/edit tool and NO
    ``mooring_investigate``, forcing depth-1 and read-only-only). ``notebook_rel`` is the
    analyst's current notebook, used as the default focus for a branch that names none.
    """
    cfg = app_cfg.ai.investigate
    if not cfg.enabled:
        return None

    from dataclasses import replace

    from mooring.ai.investigate import (
        BranchJob,
        InvestigatePlanner,
        merge_findings,
        resolve_concurrency,
    )

    # Resolve AUTO (0) concurrency HERE, where the configured provider is known — the
    # planner is provider-agnostic. An explicitly configured value always wins.
    cfg = replace(
        cfg, max_concurrency=resolve_concurrency(cfg.max_concurrency, app_cfg.ai_provider)
    )

    def run_investigation(branches: list, on_progress=None) -> str:
        jobs = [
            BranchJob(
                question=str(b.get("question", "")).strip(),
                notebook_rel=str(b.get("notebook", "")).strip(),
                dataset_rel=str(b.get("dataset", "")).strip(),
            )
            for b in branches
            if isinstance(b, dict) and str(b.get("question", "")).strip()
        ]
        if not jobs:
            return ""
        planner = InvestigatePlanner(
            config=cfg,
            pii=app_cfg.ai.pii,
            build_context=build_context,
            open_session=open_readonly_session,
            default_notebook_rel=notebook_rel,
            on_progress=on_progress,
        )
        return merge_findings(planner.run(jobs))

    return run_investigation
