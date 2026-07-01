"""The AI chat application service — the deferred P7, finally landed.

Owns what used to live inside the web adapter: the chat-session registry (and
its lock + lifecycle: close/reap/per-notebook teardown), the CONTEXT ASSEMBLY —
:meth:`ChatService.build_context` is the application's SOLE caller of
:func:`mooring.ai.egress.build_system_context`, so the value-blindness choke
point now sits next to the privacy machinery it feeds instead of among route
handlers — and the live-kernel schema pipeline. Transport stays in the hub
(JSON/SSE shapes); provider construction stays on the Hub (`_make_chat_session`
is the seam the tests stub).

Config is passed per call (``app_cfg``), never stored: the hub reloads its
config in place, and a service holding a stale snapshot would silently pin the
old workspace/guard settings.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from mooring import workspace_config
from mooring.app import notebooks


class ChatService:
    def __init__(self) -> None:
        # AI copilot chat sessions, keyed by a hub-minted sid. Each is bound to
        # one open notebook; the value is a chat.StubChatSession (Phase 0) or a
        # CopilotChatSession (Phase 1) — both ChatBroadcasters.
        self._chats: dict[str, object] = {}
        self._targets: dict[str, tuple[str, str]] = {}  # sid -> (workspace, notebook rel)
        self._lock = threading.Lock()

    # -- registry / lifecycle --------------------------------------------------

    def get(self, sid: str):
        return self._chats.get(sid)

    def target(self, sid: str) -> tuple[str, str] | None:
        with self._lock:
            return self._targets.get(sid)

    def register(self, sid: str, session, workspace: Path, notebook_rel: str) -> None:
        with self._lock:
            self._chats[sid] = session
            self._targets[sid] = (str(workspace), notebook_rel)

    def close(self, sid: str) -> None:
        """Tear down one chat session (drop its target, close the provider)."""
        with self._lock:
            session = self._chats.pop(sid, None)
            self._targets.pop(sid, None)
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._chats.values())
            self._chats.clear()
            self._targets.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    def close_for_notebook(self, workspace: Path, notebook_rel: str) -> int:
        """Close every live chat bound to one notebook. Used when AI is disabled for
        it, so a window opened before the toggle stops streaming. Returns the count."""
        want = (str(workspace), workspace_config.normalize_notebook(notebook_rel))
        with self._lock:
            sids = [
                sid
                for sid, (ws, nb) in self._targets.items()
                if (ws, workspace_config.normalize_notebook(nb)) == want
            ]
        for sid in sids:
            self.close(sid)
        return len(sids)

    def close_if_disabled(self, sid: str) -> bool:
        """The per-notebook opt-out gate shared by send/apply/rollback: if the
        session's notebook is AI-disabled, tear the session down and report True
        (the adapter answers with its 403). Re-checked at each egress (not just
        open) because the notebook may be disabled mid-session from the hub or a
        teammate's sync."""
        target = self.target(sid)
        if target and workspace_config.is_ai_disabled(Path(target[0]), target[1]):
            self.close(sid)
            return True
        return False

    def reap_idle(self, timeout: float) -> None:
        with self._lock:
            dead = [sid for sid, s in self._chats.items() if s.idle_seconds() > timeout]  # ty: ignore[unresolved-attribute]
            sessions = [self._chats.pop(sid) for sid in dead]
            for sid in dead:
                self._targets.pop(sid, None)
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    # -- context assembly (the value-blindness choke point's ONE caller) --------

    def build_context(self, app_cfg, workspace: Path, notebook_rel: str, dataset_rel: str):
        """Return ``(system_context, dictionary_index, pii_banner, live_text)``.

        The value-free core is the dataset SCHEMA + notebook SOURCE. When the
        opt-in context feature is on, it also folds in the team instructions and
        a locality-selected, value-minimised data-dictionary slice (with the
        selected dataset's schema enriched by matching dictionary entries), and
        returns the parsed index so the session can offer the pull tools.

        When the opt-in PII guard is on, it additionally withholds any schema
        column whose NAME is itself a PII value (a pivot/transpose on a PII key)
        and, with ``scan_notebook_source``, collects value-free findings for the
        notebook source into ``pii_banner`` (a one-time, warn-only UI banner —
        the source is never mutated).
        """
        from dataclasses import replace

        from mooring import schema
        from mooring.ai import context as ctxmod
        from mooring.ai import egress, locality, ner, pii
        from mooring.ai.datadictionary import DictionaryIndex

        pii_banner: list[dict] = []
        repo_ctx = ctxmod.discover_context(
            workspace,
            context_dir=app_cfg.ai_context_dir,
            enabled=app_cfg.ai_context,
            max_kb=app_cfg.ai_context_max_kb,
        )
        index = repo_ctx.index
        has_dict = not index.is_empty()

        schema_text = ""
        dataset_schema = None
        if dataset_rel:
            ds = notebooks.ws_file(workspace, dataset_rel)
            try:
                dataset_schema = schema.extract_schema(ds)
            except (ValueError, OSError) as exc:
                raise ValueError(f"Could not read the schema for {dataset_rel}: {exc}") from exc
            if app_cfg.ai_pii:
                kept, col_findings = egress.scrub_columns(dataset_schema.columns)
                if col_findings:  # a column NAME is itself a PII value — withhold it
                    dataset_schema = replace(dataset_schema, columns=kept)
                    pii_banner += [
                        {"where": f"{dataset_rel} column", "kind": f.kind} for f in col_findings
                    ]
            schema_text = (
                locality.enrich_dataset_schema(dataset_schema, index, dataset_rel)
                if has_dict
                else schema.format_for_ai(dataset_schema, source=dataset_rel)
            )

        source = notebooks.ws_file(workspace, notebook_rel, suffix=".py").read_text("utf-8")
        if app_cfg.ai_pii and app_cfg.ai_pii_scan_source:
            # Warn-only: the notebook source is the analyst's own working file, so we
            # never mutate it — we surface a value-free banner and let them decide.
            # Resolve "auto" -> concrete and shape the name model only HERE, under the
            # ai_pii gate — so a default (guard-off) install never imports spaCy just
            # to pick a backend it won't use. Consistent with the chat session.
            pii_backend = ner.resolve_backend(app_cfg.ai_pii_name_backend)
            pii_name_model = ner.model_for(
                pii_backend,
                app_cfg.ai_pii_name_model,
                app_cfg.ai_pii_name_revision,
                app_cfg.ai_pii_name_variant,
            )
            pii_banner += [
                {"where": f"{notebook_rel}:{f.line}", "kind": f.kind}
                for f in pii.scan_prose(
                    source,
                    names=app_cfg.ai_pii_names,
                    labels=app_cfg.ai_pii_name_labels,
                    threshold=app_cfg.ai_pii_name_threshold,
                    model=pii_name_model,
                    backend=pii_backend,
                )
            ]

        # Schemas of dataframes LIVE in the running kernel are DEFERRED off the open
        # path (a freshly opened notebook's kernel is often still loading frames, so
        # the probe's worst case is a multi-second poll). The very first turn picks
        # them up via the per-turn refresh (api_chat_send -> live_schema_for_sid),
        # over the SAME value-free probe -> scrub -> format pipeline, so nothing about
        # the privacy contract changes — only WHEN the probe runs. The system context
        # opens on the file-based schema; the live schema joins on turn 1.
        live_text = ""

        dictionary_text = ""
        if has_dict:
            dataset_cols = (
                {n for n, _ in dataset_schema.columns} if dataset_schema is not None else set()
            )
            stem = Path(dataset_rel).stem if dataset_rel else ""
            tables, reasons, n_more = locality.working_set(
                index,
                dataset_columns=dataset_cols,
                dataset_stem=stem,
                notebook_source=source,
                notebook_rel=notebook_rel,
            )
            dictionary_text = locality.seed_text(tables, reasons, n_more)

        context = egress.build_system_context(
            schema_text=schema_text,
            notebook_source=source,
            notebook_rel=notebook_rel,
            live_schemas_text=live_text,
            instructions_text=repo_ctx.instructions,
            dictionary_text=dictionary_text,
        )
        return context, (index if has_dict else DictionaryIndex()), pii_banner, live_text

    # -- live-kernel schema pipeline ---------------------------------------------

    def live_schema_text(self, app_cfg, editor, notebook_rel: str) -> tuple[str, list[dict]]:
        """Value-free schema of the dataframes LIVE in ``notebook_rel``'s kernel.

        ``editor`` is the (possibly None / not running) EditorServer for the
        notebook's workspace. Returns ``(rendered_text, pii_banner)``. Best-effort:
        any failure (live schema off, no running editor/session, frames not loaded,
        probe error) yields ``("", [])`` and the caller falls back to the file-based
        schema. The ONE value-free pipeline (introspect probe -> ``scrub_columns``
        -> ``format_live_schemas``) shared by chat-open and the per-turn refresh.
        """
        if not app_cfg.ai_live_schema:
            return "", []
        from dataclasses import replace

        from mooring.ai import egress, introspect

        banner: list[dict] = []
        try:
            frames = introspect.live_dataset_schemas(editor, notebook_rel)
            if app_cfg.ai_pii:
                scrubbed = []
                for fr in frames:
                    kept, ff = egress.scrub_columns(fr.columns)
                    if ff:  # a pivot/transpose put a PII value in a column NAME
                        banner += [
                            {"where": f"live `{fr.name}` column", "kind": f.kind} for f in ff
                        ]
                        fr = replace(fr, columns=kept)
                    scrubbed.append(fr)
                frames = scrubbed
            return introspect.format_live_schemas(frames), banner
        except Exception:  # noqa: BLE001  # never block chat on introspection
            return "", []

    def live_schema_for_sid(self, app_cfg, editors: dict, sid: str) -> tuple[str, list[dict]]:
        """The current live-kernel schema for an open chat session (best-effort)."""
        target = self.target(sid)
        if target is None:
            return "", []
        workspace_str, notebook_rel = target
        return self.live_schema_text(app_cfg, editors.get(workspace_str), notebook_rel)

    # -- value-free guard status ---------------------------------------------------

    def pii_status(self, app_cfg) -> dict:
        """Value-free snapshot of the outbound-PII guard for the chat UI badge: is
        the pre-flight scan on, does a hit block, and can the optional name pass
        actually run right now. Carries no finding, value, or path — only config
        booleans plus the resolved backend name."""
        enabled = bool(app_cfg.ai_pii)
        names = bool(app_cfg.ai_pii_names)
        backend = ""
        names_active = False
        if enabled and names:
            from mooring.ai import ner

            backend = ner.resolve_backend(app_cfg.ai_pii_name_backend)
            model = ner.model_for(
                backend,
                app_cfg.ai_pii_name_model,
                app_cfg.ai_pii_name_revision,
                app_cfg.ai_pii_name_variant,
            )
            names_active = bool(ner.available(backend) and ner.is_ready(model, backend))
        return {
            "enabled": enabled,
            "block": bool(app_cfg.ai_pii_block_prompt),
            "names": names,
            "names_active": names_active,
            "backend": backend,
        }
