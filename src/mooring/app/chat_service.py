"""The AI chat application service — the June review's deferred hub-thinning,
finally landed (P3 of docs/developers/architecture-plan.md).

Owns what used to live inside the web adapter: the chat-session registry (and
its lock + lifecycle: close/reap/per-notebook teardown, and the session's
optional in-turn applier beside it, so a turn boundary is one call rather than a
second registry in the routes), the CONTEXT ASSEMBLY —
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
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from mooring import checks, datasets, inputs, policy, workbook, workspace_config
from mooring.app import notebooks


# How the system context refers to the ONE notebook-write tool. A PHRASE, not a name:
# the context is built before the session exists and is shared by sessions that
# register the tool under either of its two names — and by read-only ones that have no
# write tool at all. The per-session tool guide (``ai/session.py``) names it for real,
# derived from the same helper that registers it, so nothing here has to guess.
WRITE_TOOL_PHRASE = "the notebook write tool"


@dataclass(frozen=True)
class RoutingContextBundle:
    """Two destination-specific renders of one captured context snapshot."""

    trusted: tuple
    general: tuple
    source_digest: bytes


class ChatService:
    def __init__(self) -> None:
        # AI copilot chat sessions, keyed by a hub-minted sid. Each is bound to
        # one open notebook; the value is a chat.StubChatSession (Phase 0) or a
        # CopilotChatSession (Phase 1) — both ChatBroadcasters.
        self._chats: dict[str, object] = {}
        self._targets: dict[str, tuple[str, str]] = {}  # sid -> (workspace, notebook rel)
        # sid -> the session's NotebookApplier (mooring.app.auto_apply), when the model
        # writes for itself. Absent in manual mode, where no write-through is registered
        # at all — see Hub._make_applier.
        self._appliers: dict[str, object] = {}
        self._lock = threading.Lock()

    # -- registry / lifecycle --------------------------------------------------

    def get(self, sid: str):
        return self._chats.get(sid)

    def target(self, sid: str) -> tuple[str, str] | None:
        with self._lock:
            return self._targets.get(sid)

    def register(self, sid: str, session, workspace: Path, notebook_rel: str, applier=None) -> None:
        with self._lock:
            self._chats[sid] = session
            self._targets[sid] = (str(workspace), notebook_rel)
            if applier is not None:
                self._appliers[sid] = applier

    def begin_turn(self, sid: str) -> str:
        """Tell this session's applier that a NEW turn is starting, so its writes get a
        fresh undo checkpoint and a fresh receipt group.

        A turn starts when the analyst sends — nothing else does — so this is called from
        the send paths (``/api/ai/chat/send`` and the Run & report hand-off) rather than
        being inferred inside the applier, which has no view of the conversation. Returns
        the new turn id, or ``""`` when this session has no applier (manual mode), which
        is not a condition anyone needs to handle.

        Two ways the id can end up describing the wrong span, and they are one problem
        with one answer — the checkpoint has to cover exactly the turn the Revert button
        offers to take back:

        * **A failure is not swallowed.** This used to catch everything and return
          ``""``, which reads like robustness and is not: a missed boundary leaves the
          applier on the PREVIOUS turn id, so
          :meth:`mooring.app.apply.ApplyGuard.apply_with_undo` extends that turn's
          checkpoint instead of opening a new one, and Revert silently rolls back more
          than it says. Nothing has been written when this runs (it is called before the
          send), so failing loudly costs a retry; failing quietly costs the analyst work
          they never agreed to lose. The error is logged on its way past.
        * **A turn already in flight does not rotate the id.** An analyst who types a
          second message while the assistant is still working reaches this before the
          session refuses the concurrent send — and rotating there SPLITS the running
          turn: its next write takes a second snapshot, so "undo what the assistant just
          did" undoes only the tail of it. The distinguishing fact lives in the session
          (only it knows a turn is live), so it is asked, duck-typed, and the current id
          is returned unchanged.

        The in-flight check FAILS OPEN — a session that does not answer, or answers by
        raising, rotates exactly as before — because a backend without the concept must
        behave as it always did, and a stuck "busy" answer would freeze the turn id for
        the life of the chat, which is the worse failure of the two.
        """
        with self._lock:
            applier = self._appliers.get(sid)
        begin = getattr(applier, "begin_turn", None)
        if not callable(begin):
            return ""
        if self._turn_in_flight(sid):
            return str(getattr(applier, "turn_id", "") or "")
        try:
            return str(begin() or "")
        except Exception as exc:
            from mooring import telemetry

            with contextlib.suppress(Exception):
                telemetry.log_error(exc=exc, op="ai_chat_begin_turn")
            raise

    def _turn_in_flight(self, sid: str) -> bool:
        """Whether this session says a turn of its own is still running.

        Duck-typed on ``turn_in_flight()``: only a session that tracks the thing can
        answer, and today only the routed session does (its ``_turn_idle`` event, which
        is what makes a concurrent send a refusal rather than an interleave). Anything
        that does not offer the method — or that raises — reads as "not in flight", so
        this can only ever SUPPRESS a rotation that would have split a live turn, never
        withhold one that a session actually needed.
        """
        with self._lock:
            session = self._chats.get(sid)
        in_flight = getattr(session, "turn_in_flight", None)
        if not callable(in_flight):
            return False
        try:
            return bool(in_flight())
        except Exception:  # noqa: BLE001 — an unanswerable question is not a "yes"
            return False

    def close(self, sid: str) -> None:
        """Tear down one chat session (drop its target, close the provider)."""
        with self._lock:
            session = self._chats.pop(sid, None)
            self._targets.pop(sid, None)
            self._appliers.pop(sid, None)
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._chats.values())
            self._chats.clear()
            self._targets.clear()
            self._appliers.clear()
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
        if target and policy.ai_disabled(Path(target[0]), target[1]):
            self.close(sid)
            return True
        return False

    def reap_idle(self, timeout: float) -> None:
        with self._lock:
            dead = [sid for sid, s in self._chats.items() if s.idle_seconds() > timeout]  # ty: ignore[unresolved-attribute]
            sessions = [self._chats.pop(sid) for sid in dead]
            for sid in dead:
                self._targets.pop(sid, None)
                self._appliers.pop(sid, None)
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()  # ty: ignore[unresolved-attribute]

    # -- context assembly (the value-blindness choke point's ONE caller) --------

    def build_context(
        self,
        app_cfg,
        workspace: Path,
        notebook_rel: str,
        dataset_rel: str,
        folders: tuple[str, ...] = (),
        trusted_customer_data: bool = False,
        routing_bundle: bool = False,
    ):
        """Return ``(system_context, dictionary_index, pii_banner, live_text, models,
        code_index, catalog)``.

        The value-free core is the dataset SCHEMA + notebook SOURCE. When the
        opt-in context feature is on, it also folds in the team instructions and
        a locality-selected, value-minimised data-dictionary slice (with the
        selected dataset's schema enriched by matching dictionary entries), and
        returns the parsed index so the session can offer the pull tools.

        When ``[ai] semantic_model`` is on (the default), synced Power BI
        semantic models under ``folders`` are discovered and extracted through
        the allowlist parser (:mod:`mooring.pbip_model`), models the team opted
        out (synced ``mooring.toml`` ``[ai] disabled_semantic_models``) are
        dropped, a names-only hint joins the system context, and the parsed
        ``models`` ride as the 5th tuple element so the session can offer the
        model tools. The models element is APPENDED so slice-consumers (the
        batch planner takes ``[:2]``) are unaffected — batch builder sessions
        therefore do not get model tools.

        When the opt-in PII guard is on, it additionally withholds any schema
        column whose NAME is itself a PII value (a pivot/transpose on a PII key)
        and, with ``scan_notebook_source``, collects value-free findings for the
        notebook source into ``pii_banner`` (a one-time, warn-only UI banner —
        the source is never mutated).
        """
        from dataclasses import replace

        from mooring import pbip_model, schema
        from mooring.ai import context as ctxmod
        from mooring.ai import egress, locality, ner, pii, tools
        from mooring.ai.datadictionary import DictionaryIndex
        from mooring.app import context_folders as ctxdirs

        pii_banner: list[dict] = []
        repo_ctx = ctxmod.discover_contexts(
            workspace,
            ctxdirs.read_dirs(app_cfg, workspace),
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
            if app_cfg.ai_pii and not trusted_customer_data:
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

        # Power BI semantic models: discovery + the allowlist extraction happen
        # HERE (per open, off the event loop with the rest of this method), the
        # per-model synced opt-out filters, and only a NAMES-ONLY hint enters the
        # context — the DAX detail stays behind the pull tools, out of the window.
        models: list = []
        semantic_models_text = ""
        if app_cfg.ai_semantic_model and folders:
            models_off = workspace_config.disabled_semantic_models(workspace)
            models = [
                pbip_model.extract_model(ref.path, key=ref.key, name=ref.name)
                for ref in pbip_model.find_models(workspace, tuple(folders))
                if workspace_config.normalize_notebook(ref.key) not in models_off
            ]
            semantic_models_text = pbip_model.render_models_hint(models)

        # Team code library: the value-free API skeleton of the team's importable .py
        # helper modules under the synced folders (mooring.ai.codelib — ast only, NEVER
        # imported/executed), gated on the opt-in [ai] code_index. Only a locality-selected
        # NAMES + SIGNATURES seed enters the context; the rest stays behind the helper tools,
        # and the parsed index rides as the 6th tuple element so the session offers them.
        code_index = None
        helpers_text = ""
        if app_cfg.ai_code_index and folders:
            from mooring.ai import codelib

            off = workspace_config.disabled_code_modules(workspace)
            full = codelib.load_index(
                workspace, tuple(folders), exclude=(notebook_rel, dataset_rel)
            )
            code_index = (
                replace(
                    full,
                    modules=tuple(
                        m
                        for m in full.modules
                        if workspace_config.normalize_notebook(m.import_path or m.path) not in off
                    ),
                )
                if off
                else full
            )
            hmods, hreasons, hmore = locality.helper_working_set(
                code_index, notebook_source=source, notebook_rel=notebook_rel
            )
            helpers_text = locality.helper_seed_text(hmods, hreasons, hmore)

        # The repo-wide notebook catalog: every marimo notebook reduced (by ast, never
        # executed and never from a .mooring receipt) to its H1 title + declared
        # inputs/checks/imports/SQL tables. It rides as the 7th tuple element and reaches
        # the model ONLY through the on-demand catalog tools — nothing enters the system
        # context, because a listing scales with the repo and would be paid on every turn
        # even when the analyst never asks "has someone built this?". Opt-in (the H1 title
        # is authored prose, so this belongs in the same tier as context/code_index).
        #
        # `and folders` mirrors the code library's gate, and does real work here: the batch
        # planner's build_context deliberately passes NO folders (see hub/server.py) because
        # its [:2] slice discards everything past the dictionary — without this, every batch
        # job would re-parse the whole repo to build an index nobody reads.
        #
        # The team's per-notebook AI opt-out is applied HERE: a notebook the team fenced off
        # must not become searchable metadata either. Note the snapshot semantics — the
        # catalog is built once per chat-open, so a notebook fenced off mid-session stays
        # searchable until the chat is reopened (the semantic-model tools behave the same).
        catalog = None
        if app_cfg.ai_notebook_catalog and folders:
            from mooring.ai import notebookindex
            from mooring.ai.notebookindex import prosescan

            # The title is the one authored-prose slot, and this is the egressing path, so
            # give it the operator's FULL scanner (NER name pass included) rather than the
            # structured-only default the local hub listing uses.
            title_scan = prosescan.scan_title
            if app_cfg.ai_pii:
                cat_backend = ner.resolve_backend(app_cfg.ai_pii_name_backend)
                title_scan = prosescan.make_scanner(
                    names=app_cfg.ai_pii_names,
                    labels=app_cfg.ai_pii_name_labels,
                    threshold=app_cfg.ai_pii_name_threshold,
                    model=ner.model_for(
                        cat_backend,
                        app_cfg.ai_pii_name_model,
                        app_cfg.ai_pii_name_revision,
                        app_cfg.ai_pii_name_variant,
                    ),
                    backend=cat_backend,
                )
            # The per-notebook opt-out AND the policy's ai_off globs (policy.ai_gate
            # unions them), so a path an admin fenced off never enters the repo-wide
            # catalog the copilot can search — the widest AI surface there is.
            catalog = notebookindex.load_catalog(
                workspace,
                tuple(folders),
                exclude_fn=policy.ai_gate(workspace),
                scan=title_scan,
            )

        context_args = dict(
            schema_text=schema_text,
            notebook_source=source,
            notebook_rel=notebook_rel,
            live_schemas_text=live_text,
            instructions_text=repo_ctx.instructions,
            dictionary_text=dictionary_text,
            semantic_models_text=semantic_models_text,
            helpers_text=helpers_text,
            # Let the copilot AUTHOR value-free tie-out checks on request. This is a
            # static capability note (the mooring_checks API), never a receipt or a
            # value — so it opens no new egress channel.
            checks_help=checks.copilot_guide(),
            # Likewise, let it author marimo SQL (mo.sql / DuckDB) cells — authored code
            # the model never sees the result of, so no new egress channel either.
            #
            # The write tool is NOT named here, and that is the fix for a real bug: this
            # is the SOLE context builder for three different kinds of session — the
            # interactive chat (write tool in propose OR edit mode, depending on whether
            # the hub wired an applier), the BATCH planner (propose mode, always: it is
            # given no applier), and the read-only INVESTIGATE sub-agents (no write tool
            # at all). `[ai] auto_apply` answers for the first of those and for neither
            # of the others, so keying the guide off the config told a batch session to
            # call `mooring_edit_notebook`, which it does not have. The only place that
            # knows a session's actual write capability is the session
            # (`ai/session.py`'s per-session tool guide names it from the same
            # `write_tool_name(applier is not None)` that REGISTERS it, on every turn),
            # so this note stays mode-neutral rather than guessing a name.
            sql_help=tools.sql_cell_guide(WRITE_TOOL_PHRASE),
            # And author value-free input/output fingerprints (mooring_inputs) on request
            # — a hash/shape/schema receipt, never a value, so no new egress channel. The
            # guide DESCRIBES the API; the receipts themselves (and the lineage graph
            # derived from them) are never read into the model's context.
            inputs_help=inputs.copilot_guide(),
            # And author the Excel-delivery cell (mooring_deliver) on request — sheet
            # names and frames it can already see in the source, so no new channel either.
            workbook_help=workbook.copilot_guide(),
            # The team's value-free connection SHAPES (names + fields, never the secret),
            # so the copilot can write connection code that references them.
            connections_help=workspace_config.connections_hint(workspace),
            # The team's dataset POINTERS — names + file formats only, so the copilot can
            # write `md.path("sales")` wiring without ever learning where the file lives.
            datasets_help=datasets.copilot_guide(workspace),
        )
        tail = (
            (index if has_dict else DictionaryIndex()),
            pii_banner,
            live_text,
            models,
            code_index,
            catalog,
        )
        if routing_bundle:
            # Both renders use the exact same source/schema/team/index snapshot. A
            # second context build could race edits and put unclassified content in
            # the general render.
            trusted_context = egress.build_system_context(
                **context_args, trusted_customer_data=True
            )
            general_context = egress.build_system_context(
                **context_args, trusted_customer_data=False
            )
            return RoutingContextBundle(
                trusted=(trusted_context, *tail),
                general=(general_context, *tail),
                source_digest=hashlib.sha256(source.encode("utf-8")).digest(),
            )
        context = egress.build_system_context(
            **context_args, trusted_customer_data=trusted_customer_data
        )
        return (context, *tail)

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
