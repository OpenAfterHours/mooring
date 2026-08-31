"""AI copilot chat endpoints: open/stream/send/apply/rollback, the value-free
dataset+model listings, Copilot sign-in, and the per-notebook AI toggle."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import tomllib
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mooring import policy, telemetry, workspace_config
from mooring.hub.sse import chat_replay, event_stream, sse_response


def _unknown_session() -> JSONResponse:
    from mooring.hub.server import _UNKNOWN_CHAT_SESSION

    return JSONResponse({"error": _UNKNOWN_CHAT_SESSION}, status_code=404)


async def api_chat_open(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    data = await request.json()
    notebook = str(data.get("notebook", "")).strip()
    dataset = str(data.get("dataset", "")).strip()
    model = str(data.get("model", "")).strip()
    requested_trusted_model = str(data.get("trusted_model", "")).strip()
    requested_routing_preference = str(data.get("routing_preference", "")).strip().lower()
    # Capture one coherent starting policy. The context/session work below can be
    # slow; immediately before registration the same snapshot is checked again
    # under this lock so reload/AI-off cannot strand a late stale session.
    with hub._lock:
        if not hub.app_cfg.ai_enabled:
            return JSONResponse({"enabled": False}, status_code=404)
        opening_app_cfg = hub.app_cfg
        opening_policy = hub._chat_open_policy_snapshot()
        workspace = hub.cfg.workspace()
        routing_enabled = opening_app_cfg.ai_routing_enabled
    # An explicit pick from the effort picker wins; "" means the page had no picker
    # to offer (a model that takes no effort, or a provider that advertises none), so
    # the configured default stands in. A page WITH a picker always sends a concrete
    # word — including "default", the sentinel that means "send no effort at all" —
    # because /api/ai/models offers the configured value in the list (see
    # _offer_configured_effort), so this fallback can no longer swallow it.
    reasoning_effort = (
        str(data.get("reasoning_effort", "")).strip()
        or opening_app_cfg.ai_reasoning_effort
    )
    trusted_model = ""
    routing_preference = "auto"
    profile_label = ""
    try:
        if routing_enabled:
            requested_trusted_model = (
                requested_trusted_model or opening_app_cfg.ai_default_trusted_model
            )
            requested_routing_preference = (
                requested_routing_preference or opening_app_cfg.ai_routing_preference
            )
            trusted_model, routing_preference, profile_label = hub._trusted_chat_options(
                requested_trusted_model, requested_routing_preference
            )
        elif requested_routing_preference not in {"", "auto"} or requested_trusted_model:
            raise ValueError("Approved routing is not enabled; remove trusted routing options.")
    except ValueError as exc:
        # Browser-controlled values are rejected before the notebook context is
        # read or either approved AI client can be constructed.
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 - managed profile/configuration failure
        return JSONResponse({"error": str(exc)}, status_code=502)
    if not notebook:
        return JSONResponse({"error": "A notebook is required."}, status_code=400)
    # Per-notebook opt-out (synced mooring.toml). 403 + reason distinguishes
    # this from the global-off 404 above, so the chat UI shows the right message.
    if policy.ai_disabled(workspace, notebook):
        return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
    try:
        # File IO (notebook source, dataset schema, team context, semantic-model
        # extraction) — off the event loop so a slow read can't stall the hub's
        # other requests.
        if routing_enabled:
            bundle = await run_in_threadpool(
                hub._build_chat_context,
                workspace,
                notebook,
                dataset,
                routing_bundle=True,
            )
            context, index, _pii_banner, live_text, models, code_index, catalog = bundle.trusted
            pii_banner = []
        else:
            bundle = None
            ctx = await run_in_threadpool(hub._build_chat_context, workspace, notebook, dataset)
            context, index, pii_banner, live_text, models, code_index, catalog = ctx
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": f"No such file: {exc}"}, status_code=404)
    hub._reap_idle_chats()
    # The model's in-turn writer, or None when `[ai] auto_apply` is off — in which case
    # NOTHING is wired and the write tool stays in propose mode. Built before the session
    # because it is an argument to it, and bound to the session afterwards (it needs the
    # cancel flag, which cannot exist before the session does).
    applier = hub._make_applier(workspace, notebook)
    try:
        if bundle is not None:
            session = await run_in_threadpool(
                hub._make_routed_chat_session,
                bundle,
                workspace,
                notebook,
                dataset,
                model=model,
                reasoning_effort=reasoning_effort,
                trusted_model=trusted_model,
                routing_preference=routing_preference,
                applier=applier,
            )
        else:
            session = hub._make_chat_session(
                context,
                workspace,
                notebook,
                model=model,
                reasoning_effort=reasoning_effort,
                dictionary=index,
                semantic_models=models,
                helpers=code_index,
                catalog=catalog,
                applier=applier,
            )
    except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI in Phase 1
        return JSONResponse({"error": str(exc)}, status_code=502)
    if applier is not None:
        # Late-bound on purpose (see above). For a routed chat this is the WRAPPER, which
        # is what `request_cancel` reaches whichever child is live.
        applier.bind(session)
    # The live-kernel schema is deferred off the open path (see _build_chat_context),
    # so live_text is ""; the first turn picks it up. This seeds the (empty) snapshot.
    session.set_initial_live_schema(live_text)
    # Kick off the (one-time) NER model download in the background with progress,
    # so name detection doesn't hang the first chat turn silently.
    session.prepare_pii_model()
    sid = secrets.token_urlsafe(9)
    refusal = hub._register_chat_if_policy_current(
        sid,
        session,
        workspace,
        notebook,
        policy_snapshot=opening_policy,
        routing_enabled=routing_enabled,
        trusted_model=trusted_model,
        routing_preference=routing_preference,
        profile_label=profile_label,
        applier=applier,
    )
    if refusal:
        # The session was never registered, so lifecycle cleanup is ours. Keep
        # provider/credential details out of the race response.
        with contextlib.suppress(Exception):
            session.close()
        if refusal == "notebook_disabled":
            return JSONResponse(
                {"enabled": False, "reason": "notebook_disabled"}, status_code=403
            )
        return JSONResponse(
            {"error": "AI configuration changed while this chat was opening. Retry."},
            status_code=409,
        )
    telemetry.log_event("ai_chat_open")
    if pii_banner:  # count only — never a kind/value reaches the central sink
        telemetry.log_event("ai_pii", findings=len(pii_banner))
    # Which profile answers: "managed" (a firm-approved endpoint) or "local" (one the
    # user configured). Omitted when unknown, exactly as the SSE "routing" event omits
    # it, so the browser reads ONE shape from both.
    profile_source = str(getattr(session, "profile_source", "") or "")
    return JSONResponse(
        {
            "sid": sid,
            "notebook": notebook,
            "pii": pii_banner,
            # Approved routing supersedes the legacy local PII hold. ``null`` is
            # intentional: reporting ``enabled: false`` made the UI imply that
            # protection had been switched off rather than replaced.
            "guard": None if bundle is not None else hub._pii_status(),
            "route": (
                {
                    "zone": session.zone,
                    **(
                        {
                            "profile_label": session.profile_label,
                            "model": session.trusted_model,
                            **({"source": profile_source} if profile_source else {}),
                        }
                        if session.zone == "trusted"
                        else {}
                    ),
                }
                if bundle is not None
                else None
            ),
            "trusted_model": trusted_model if bundle is not None else None,
            "routing_preference": routing_preference if bundle is not None else None,
            # Whether the chat is usable NOW. A backgrounded provider session is
            # still starting (Copilot handshake) — the UI shows "connecting…" and
            # waits for the "ready"/"fail" event on the stream. The stub/already-
            # ready sessions report True and the UI enables the input immediately.
            "ready": session.is_ready(),
        }
    )


def api_chat_stream(request: Request) -> StreamingResponse | JSONResponse:
    # Sync: this handler only builds the StreamingResponse; the awaiting happens
    # inside the shared event_stream generator it wraps.
    hub = request.app.state.hub
    sid = request.path_params["sid"]
    session = hub.chat.get(sid)
    if session is None:
        return _unknown_session()
    # The replay is a callable: event_stream computes it AFTER subscribing, so a
    # readiness flip can't fall between the snapshot and the subscription.
    return sse_response(event_stream(session, lambda: chat_replay(session)))


async def api_chat_send(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    sid = str(data.get("sid", ""))
    session = hub.chat.get(sid)
    if session is None:
        return _unknown_session()
    # Refresh the live-kernel schema so dataframes added since chat-open (or the
    # last turn) are visible without reopening. Value-free + best-effort; the
    # session re-injects it only when it changed. Off-thread — it does kernel I/O.
    live_text, live_banner = await asyncio.to_thread(hub._live_schema_for_sid, sid)
    if live_banner:  # a refreshed column NAME was itself PII (withheld) — count only
        telemetry.log_event("ai_pii", findings=len(live_banner))
    # The notebook may have been disabled (from the hub, or a teammate's sync)
    # since this window opened — re-check at the LATEST point before egress. The
    # live-schema probe above can take real time (a kernel poll), a wide window;
    # this _chat_targets re-check, not the hidden button, is the real guarantee.
    if (blocked := hub._disabled_block(sid)) is not None:
        return blocked
    # A turn is starting. Everything the model writes from here shares ONE undo
    # checkpoint and ONE receipt group, because "undo the assistant's last turn" is the
    # unit an analyst thinks in — not "undo its fourth write". Nothing happens in manual
    # mode (no applier), and the id is deliberately minted BEFORE the send rather than
    # inferred later: only a send starts a turn.
    hub.chat.begin_turn(sid)
    # "Send anyway" path: forward a prompt the PII guard held, verbatim, once.
    confirm = str(data.get("confirm_token", "")).strip()
    if confirm:
        try:
            await asyncio.to_thread(session.send_confirmed, confirm, live_text)  # ty: ignore[unresolved-attribute]
        except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI
            return JSONResponse({"error": str(exc)}, status_code=502)
        telemetry.log_event("ai_chat_send", confirmed=1)
        return JSONResponse({"ok": True, "pii": live_banner})
    text = str(data.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "Type a message."}, status_code=400)
    try:
        await asyncio.to_thread(session.send, text, live_text)  # ty: ignore[unresolved-attribute]
    except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI in Phase 1
        return JSONResponse({"error": str(exc)}, status_code=502)
    telemetry.log_event("ai_chat_send")
    return JSONResponse({"ok": True, "pii": live_banner})


async def api_chat_cancel(request: Request) -> JSONResponse:
    """Stop the turn in flight.

    The counterweight to the whole feature: with the model writing, running and
    re-checking its own work, a hard analysis is meant to run as long as it takes — so
    the analyst's control is not an iteration cap, it is this. It raises the session's
    portable flag (every tool call from now on comes back as a terminal "cancelled", and
    the applier refuses to write) and, on the Copilot backend, additionally asks the SDK
    to end the completion it is processing right now.

    Deliberately unconditional: no per-notebook gate, no policy check. Stopping is the
    one action that can only ever do LESS, and a Cancel that could be refused would be
    worse than none. Off the event loop because ``request_cancel`` blocks briefly on the
    SDK's abort round-trip.
    """
    hub = request.app.state.hub
    data = await request.json()
    sid = str(data.get("sid", ""))
    session = hub.chat.get(sid)
    if session is None:
        return _unknown_session()
    await asyncio.to_thread(session.request_cancel)
    telemetry.log_event("ai_chat_cancel")
    return JSONResponse({"ok": True})


async def api_chat_apply(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    sid = str(data.get("sid", ""))
    target = hub.chat.target(sid)
    if target is None:
        return _unknown_session()
    # Apply WRITES the notebook, so it is the highest-value gate. This early
    # refusal covers the common case; the apply guard re-checks under its lock
    # right before the write to close the toggle/write race (app/apply.py).
    if (blocked := hub._disabled_block(sid)) is not None:
        return blocked
    # The UI echoes the proposal's normalized ops; a bare ``code`` (the append
    # proposal, and the legacy contract) is normalized to a one-op append. The
    # write re-validates each edit/delete anchor against the file, so a stale
    # proposal becomes a loud 409 rather than a silent clobber.
    ops = data.get("ops")
    if isinstance(ops, list) and ops:
        op_dicts = ops
    else:
        code = str(data.get("code", ""))
        if not code.strip():
            return JSONResponse({"error": "Nothing to apply."}, status_code=400)
        op_dicts = [{"op": "append", "code": code}]
    # The apply gate's confirmation, echoed back on the re-POST. A token only — the
    # server re-scans and re-derives it, so this can never assert a verdict.
    gate_token = str(data.get("gate_token", "")).strip() or None
    workspace_str, notebook_rel = target
    workspace = Path(workspace_str)
    from mooring.ai.cellwrite import CellApplyConflict, CellWriteError
    from mooring.app.apply import ApplyGateHeld, scan_report

    try:
        nb_path = hub._ws_file(workspace, notebook_rel, suffix=".py")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
    # Snapshot the pre-edit bytes (for Undo), then rewrite the .py; the editor's
    # --watch picks it up and (with watcher_on_save=autorun) re-runs the changed
    # cells, so the change appears in the open notebook tab.
    try:
        undo_depth = await asyncio.to_thread(
            hub.apply.apply_with_undo,
            nb_path,
            workspace,
            notebook_rel,
            op_dicts,
            gate_token=gate_token,
        )
    except PermissionError:  # disabled between the gate above and the write
        hub.chat.close(sid)
        return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
    except ApplyGateHeld as held:
        # 428 Precondition Required — 409 already means CellApplyConflict here. Nothing
        # was written, so the client re-POSTs this same body plus the token to proceed.
        # Count + band only: the central sink never carries kinds (see ai_pii above).
        telemetry.log_event(
            "ai_chat_apply_held",
            band=held.verdict.band,
            findings=len(held.verdict.findings),
        )
        return JSONResponse(held.payload(), status_code=428)
    except CellApplyConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except CellWriteError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    band, kinds = scan_report(op_dicts)
    telemetry.log_event("ai_chat_apply", band=band, findings=len(kinds))
    # The LOCAL ledger gets the kinds themselves ("what did I approve?"); they are
    # value-free slugs from codeguard's fixed table, never anything from the cell. No
    # band beside them — the kinds imply it, and a clean apply records neither (an
    # empty list is dropped by activity.record).
    hub._activity("ai_apply", path=notebook_rel, kinds=list(kinds))
    return JSONResponse({"ok": True, "can_undo": undo_depth > 0, "undo_depth": undo_depth})


async def api_chat_run_report(request: Request) -> JSONResponse:
    """Smoke-run the chat's notebook and report any failure back to the assistant.

    The gap this closes: mooring never opens a marimo websocket (that is the channel
    carrying cell OUTPUTS), so it cannot see that an applied cell blew up at runtime — and
    the failures a weak model actually produces are exactly the ones only a RUN reveals. So
    the analyst gets one explicit action that runs the existing value-free verify smoke path
    (:mod:`mooring.app.run_report`) and hands the assistant the sanitised failure summary.

    **This ENDPOINT is never automatic.** It re-executes every cell in the notebook, so it
    fires only on a click, on a button that says so first; nothing here is reachable from a
    timer or a page load. (:mod:`mooring.app.run_report` does now have an automatic sibling
    — the model's own write asks for the same run when its observation says a cell did not
    complete — but that path is gated on a ``clean`` codeguard band and its own config knob,
    and it never comes through this route. See that module's docstring.)

    Gated exactly like apply/rollback: the per-notebook opt-out (unioned with the policy's
    ``ai_off`` globs by ``policy.ai_gate``) is checked here AND re-checked in the app layer
    immediately before the send, because the run itself takes minutes and the send is the
    egress. The run is off the event loop — it spawns a marimo kernel."""
    hub = request.app.state.hub
    data = await request.json()
    sid = str(data.get("sid", ""))
    session = hub.chat.get(sid)
    target = hub.chat.target(sid)
    if session is None or target is None:
        return _unknown_session()
    if (blocked := hub._disabled_block(sid)) is not None:
        return blocked
    workspace_str, notebook_rel = target
    if Path(workspace_str) != hub.cfg.workspace():
        # The hub switched repos under this window; running would execute a notebook in a
        # workspace this session was never opened against.
        return JSONResponse(
            {"error": "The workspace changed since this chat opened — reopen the copilot."},
            status_code=409,
        )
    from mooring.app import run_report

    # The report arrives as a new turn, so it opens a new undo checkpoint: anything the
    # model writes in reply to it must not fold into the turn that came before.
    hub.chat.begin_turn(sid)
    try:
        # The live-kernel schema is deliberately NOT refreshed for this turn: it comes from
        # the EDITOR's session, which this headless export run does not touch, and a kernel
        # poll would only add latency to an action already measured in minutes.
        report = await asyncio.to_thread(run_report.run_and_report, session, hub.cfg, notebook_rel)
    except PermissionError:  # disabled while the run was in flight
        hub.chat.close(sid)
        return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
    except run_report.ReportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except (ValueError, FileNotFoundError) as exc:  # bad/absent path (notebooks.ws_file)
        return JSONResponse({"error": str(exc)}, status_code=400)
    telemetry.log_event("ai_chat_run_report", ok=int(report.ran_clean))
    return JSONResponse(
        {
            "ok": True,
            "ran_clean": report.ran_clean,
            "cells_failed": report.cells_failed,
            # The EXACT text the assistant was given, echoed back so the analyst can read
            # what left their machine. The click is the consent; this is the receipt.
            "sent": report.sent,
            # Value-free (line, kind) pairs: what the rewrite withheld, never what it was.
            "redactions": [{"line": line, "kind": kind} for line, kind in report.redactions],
        }
    )


async def api_chat_rollback(request: Request) -> JSONResponse:
    hub = request.app.state.hub
    data = await request.json()
    sid = str(data.get("sid", ""))
    target = hub.chat.target(sid)
    if target is None:
        return _unknown_session()
    # Rollback WRITES the notebook (restores a snapshot), so it is gated by the
    # per-notebook opt-out exactly like apply — otherwise a disabled notebook
    # could still be rewritten through the undo path.
    if (blocked := hub._disabled_block(sid)) is not None:
        return blocked
    workspace_str, notebook_rel = target
    workspace = Path(workspace_str)
    try:
        nb_path = hub._ws_file(workspace, notebook_rel, suffix=".py")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": f"No such notebook: {notebook_rel}"}, status_code=404)
    try:
        remaining = await asyncio.to_thread(
            hub.apply.restore_undo, nb_path, workspace, notebook_rel
        )
    except OSError as exc:  # e.g. the file is momentarily locked — the snapshot is kept
        return JSONResponse({"error": f"Could not restore the notebook: {exc}"}, status_code=502)
    if remaining is None:
        return JSONResponse({"ok": False, "error": "Nothing to undo."}, status_code=400)
    telemetry.log_event("ai_chat_rollback")
    hub._activity("ai_rollback", path=notebook_rel)
    return JSONResponse({"ok": True, "can_undo": remaining > 0, "undo_depth": remaining})


def api_chat_datasets(request: Request) -> JSONResponse:
    """The value-free dataset PATHS for the chat's @-mention autocomplete, plus
    the current theme. A LIGHT alternative to /api/state, which (when logged in)
    makes GitHub sync round-trips this window doesn't need. Sync def -> Starlette
    runs it in a threadpool, so the directory walk never blocks the event loop."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    from mooring import schema

    cfg = hub.cfg
    datasets = schema.list_datasets(cfg.workspace(), cfg.folders)
    return JSONResponse({"datasets": datasets, "ui_theme": hub.app_cfg.ui_theme})


def _offer_configured_effort(models: list[dict], configured: str) -> list[dict]:
    """``models`` with the configured ``ai.reasoning_effort`` unioned into every
    non-empty ``efforts`` list.

    A provider advertises a FIXED list (Copilot's per-model metadata, OpenAI's
    advisory one), but ``ai.reasoning_effort`` is free text — ``minimal`` and
    ``xhigh`` are real OpenAI values the list omits, and a gateway may take its own.
    Without this the picker cannot show such a value, so it selects something else
    and the configured knob is silently discarded (it would still be displayed on
    the Settings page while nothing sent it). Unioning here rather than in the
    provider keeps the provider config-blind.

    An EMPTY list is left empty: that is the provider saying "this model takes no
    effort", and the picker must stay hidden for it. Order is preserved and the new
    value appended, so a sentinel first element ("default" = send none) stays first.
    """
    effort = (configured or "").strip()
    if not effort:
        return models
    out = []
    for model in models:
        efforts = model.get("efforts") or []
        if efforts and effort not in efforts:
            # Copy: providers CACHE the dicts they return, so mutating one would
            # poison the cache (and re-append on every later request).
            model = dict(model, efforts=[*efforts, effort])
        out.append(model)
    return out


async def api_chat_models(request: Request) -> JSONResponse:
    """The models the user can pick, plus the configured defaults (value-free)."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    provider = hub._provider_for()
    models = await asyncio.to_thread(provider.list_models)
    default_effort = hub.app_cfg.ai_reasoning_effort or ""
    payload = {
        "models": _offer_configured_effort(models, default_effort),
        "default_model": hub.app_cfg.ai_model or "",
        "default_effort": default_effort,
        # The picker's stored preference is namespaced per provider: the same effort
        # words mean different money on different backends, so a pick made under one
        # must never select under another (chat.js/batch.js -> ChatCore.effortKey).
        "provider": getattr(provider, "name", "") or hub.app_cfg.ai_provider or "",
        "preference_scope": hub._ai_preference_scope(),
        "routing": {"enabled": False},
    }
    if hub.app_cfg.ai_routing_enabled:
        try:
            payload["routing"] = hub._trusted_routing_metadata()
        except Exception:  # noqa: BLE001 - keep the general picker usable
            # An incomplete deployment profile yields no selectable model and no
            # sensitive configuration detail. Chat-open will surface the managed
            # configuration error if a caller nevertheless attempts routed chat.
            payload["routing"] = {
                "enabled": True,
                "source": hub.app_cfg.ai_routing_source,
                "profile_label": hub.app_cfg.ai_trusted_profile_label,
                "trusted_models": [],
                "managed_default_trusted_model": "",
                "default_trusted_model": "",
                "default_routing_preference": "trusted",
                "error": "The customer-data profile is unavailable.",
            }
    # When the list is empty because the provider REJECTED the request (e.g. a
    # 403 "not authorized to use this Copilot feature" — a signed-in but
    # unlicensed account), pass the reason through so the page can show it
    # instead of a silently empty picker. Value-free (a provider error string).
    error = getattr(provider, "models_error", lambda: "")()
    if error and not models:
        payload["error"] = error
    return JSONResponse(payload)


# -- Copilot sign-in ------------------------------------------------------------
# GitHub Copilot signs in SEPARATELY from mooring's GitHub login (auth.py): a
# different OAuth flow, a different credential store (~/.copilot), and possibly
# a different GitHub account. These endpoints expose that sign-in in the UI so a
# user never has to drop to `mooring ai login` in a terminal.


def api_ai_status(request: Request) -> JSONResponse:
    """Copilot sign-in status for the hub/chat. Default returns the CACHED status
    (never spawns the 150 MB CLI on a hub poll); ``?probe=1`` forces a real check.

    Sync def => Starlette runs it in a threadpool, so the forced probe's CLI spawn
    never blocks the event loop."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    provider = hub._provider_for()
    probe = request.query_params.get("probe", "").lower() in ("1", "true", "yes")
    if probe and hasattr(provider, "status"):
        st = provider.status(force=True)
        # A forced check also re-lists models, so an AUTHORIZATION failure
        # (signed in, but the account can't actually USE Copilot) is current —
        # status()'s auth probe alone reports "connected" for such an account.
        if hasattr(provider, "list_models"):
            provider.list_models(force=True)
    else:
        st = provider.cached_status() if hasattr(provider, "cached_status") else None
    data = hub._ai_status_dict(st)
    # Surface "signed in but not authorized for Copilot" so the menu (which has
    # the Switch account button) can tell the user how to fix access.
    authz = getattr(provider, "models_error", lambda: "")()
    if authz:
        data["authz_error"] = authz
    return JSONResponse(data)


async def api_ai_key_set(request: Request) -> JSONResponse:
    """Store an OpenAI API key from the hub (OS credential store) and re-probe.

    The OpenAI analogue of the Copilot device-flow sign-in: OpenAI has no browser
    flow, so the user supplies a key instead. It is a SECRET kept per-machine (the
    keyring — never the synced mooring.toml), mirroring ``mooring ai key set``.
    Returns the fresh connection status so the UI flips to connected without a
    reload. Only meaningful for the OpenAI provider."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    if (hub.app_cfg.ai_provider or "").strip().lower() != "openai":
        return JSONResponse(
            {"error": "Setting an API key applies only to the OpenAI provider."},
            status_code=400,
        )
    data = await request.json() if await request.body() else {}
    key = str(data.get("key", "")).strip()
    if not key:
        return JSONResponse({"error": "No API key provided."}, status_code=400)
    from mooring.ai import openai_provider

    try:
        await run_in_threadpool(openai_provider.save_api_key, key)
    except Exception as exc:  # noqa: BLE001  # no credential store / backend error
        return JSONResponse({"error": str(exc)}, status_code=500)
    provider = hub._provider_for()
    st = await run_in_threadpool(provider.status, True) if hasattr(provider, "status") else None
    telemetry.log_event("ai_key_set")
    return JSONResponse({"ok": True, "status": hub._ai_status_dict(st)})


async def api_ai_trusted_key_set(request: Request) -> JSONResponse:
    """Store (or clear) the API key for a SELF-CONFIGURED customer-data endpoint.

    Deliberately separate from ``/api/ai/key``: that one holds the general OpenAI
    credential, and the customer-data route must never be satisfied by it (see
    ``openai_provider.resolve_trusted_api_key``). It also does not care which
    provider answers general chat — a firm can route customer data through Azure
    while ordinary chat stays on Copilot.

    Refused outright when the deployment pins routing through the environment: there
    the credential is the launcher's to supply, not the analyst's.
    """
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    if hub.app_cfg.ai.routing.managed_pinned:
        return JSONResponse(
            {
                "error": "Customer-data routing is managed by this deployment; its "
                "credential comes from the environment."
            },
            status_code=400,
        )
    data = await request.json() if await request.body() else {}
    from mooring.ai import openai_provider

    if bool(data.get("clear")):
        await run_in_threadpool(openai_provider.delete_trusted_api_key)
        telemetry.log_event("ai_trusted_key_cleared")
        return JSONResponse({"ok": True, "stored": False})
    key = str(data.get("key", "")).strip()
    if not key:
        return JSONResponse({"error": "No API key provided."}, status_code=400)
    try:
        await run_in_threadpool(openai_provider.save_trusted_api_key, key)
    except Exception as exc:  # noqa: BLE001  # no credential store / backend error
        return JSONResponse({"error": str(exc)}, status_code=500)
    telemetry.log_event("ai_trusted_key_set")
    return JSONResponse({"ok": True, "stored": True})


async def api_ai_login_start(request: Request) -> JSONResponse:
    """Kick off the Copilot browser sign-in (device flow) in the background.

    Returns immediately; the client polls ``/api/ai/login/poll`` until the user
    has authorised in the browser. ``host`` (optional) targets a GHE Copilot."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    data = await request.json() if await request.body() else {}
    host = str(data.get("host", "")).strip() or None
    provider = hub._provider_for()
    if not hasattr(provider, "connect"):
        return JSONResponse(
            {"error": "This AI provider has no interactive sign-in."}, status_code=400
        )
    try:
        st = await run_in_threadpool(provider.connect, host)
    except Exception as exc:  # noqa: BLE001  # AIError/OSError surface to the UI
        return JSONResponse({"error": str(exc)}, status_code=502)
    telemetry.log_event("ai_login_start")
    return JSONResponse({"ok": True, "detail": st.detail})


def api_ai_login_poll(request: Request) -> JSONResponse:
    """Poll the in-progress Copilot sign-in. ``pending`` while the CLI is still
    running (browser open), then a real status probe confirms the outcome.

    Sync def => threadpool, so the final probe's CLI spawn is off the loop."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    provider = hub._provider_for()
    state = (
        provider.login_state()
        if hasattr(provider, "login_state")
        else {"running": False, "output": []}
    )
    if state.get("running"):
        return JSONResponse({"status": "pending", "output": state.get("output", [])})
    # The login process has exited — confirm with a real (forced) probe.
    st = provider.status(force=True) if hasattr(provider, "status") else None
    if st is not None and st.connected:
        telemetry.log_event("ai_login")
        return JSONResponse({"status": "ok", "account": st.account or ""})
    return JSONResponse(
        {
            "status": "error",
            "detail": (st.detail if st is not None else "") or "Copilot sign-in didn't complete.",
            "output": state.get("output", []),
        }
    )


async def api_notebook_ai_toggle(request: Request) -> JSONResponse:
    """Turn the copilot off (or back on) for ONE notebook. Writes the synced
    mooring.toml opt-out so the decision travels to teammates, and tears down any
    open chat window for that notebook when disabling. Backs both the hub-row
    toggle and the chat window's off-switch."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    data = await request.json()
    notebook = str(data.get("notebook", "")).strip()
    disabled = bool(data.get("disabled", True))
    if not notebook:
        return JSONResponse({"error": "A notebook is required."}, status_code=400)
    workspace = hub.cfg.workspace()
    # Validate the path is safe and a notebook, but do NOT require it to exist:
    # disabling should work for a notebook not pulled yet, and re-enabling must
    # stay possible after the file was renamed/deleted (to clear a stale opt-out).
    # _ws_file runs its traversal/.py checks before the is_file check, so a
    # FileNotFoundError here means "safe path, just absent" — which is fine.
    try:
        hub._ws_file(workspace, notebook, suffix=".py")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        pass
    try:
        await run_in_threadpool(workspace_config.set_ai_disabled, workspace, notebook, disabled)
    except tomllib.TOMLDecodeError:
        return JSONResponse(
            {"error": "mooring.toml is malformed — fix it before changing AI settings."},
            status_code=409,
        )
    closed = (
        await run_in_threadpool(hub._close_chats_for_notebook, workspace, notebook)
        if disabled
        else 0
    )
    telemetry.log_event("ai_notebook_toggle", disabled=int(disabled))
    return JSONResponse(
        {"ok": True, "notebook": notebook, "ai_disabled": disabled, "closed_sessions": closed}
    )


async def api_model_ai_toggle(request: Request) -> JSONResponse:
    """Turn the copilot's semantic-model access off (or back on) for ONE Power BI
    model. Writes the synced mooring.toml opt-out ([ai] disabled_semantic_models,
    keyed by the PBIP artifact key, e.g. "reports/Sales") so the decision travels
    to teammates — the artifact-row action in the hub calls this.

    NEXT-OPEN semantics, by design: the model tools are bound at session creation
    (build_tools runs once, in _aopen), and unlike the per-notebook opt-out there
    is no session registry keyed by model to tear down — so disabling a model
    takes effect for chats opened AFTER the toggle; already-open windows keep
    their tools until closed or reaped.
    """
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    data = await request.json()
    model = str(data.get("model", "")).strip()
    disabled = bool(data.get("disabled", True))
    if not model:
        return JSONResponse({"error": "A model is required."}, status_code=400)
    workspace = hub.cfg.workspace()
    # Validate the key resolves under the workspace (no traversal/absolute paths),
    # but do NOT require the model dir to exist: disabling must work before the
    # first pull, and re-enabling after a rename/delete (to clear a stale opt-out).
    key = workspace_config.normalize_notebook(model)
    try:
        target = (workspace / key).resolve()
        target.relative_to(workspace.resolve())
    except (ValueError, OSError):
        return JSONResponse({"error": "Path escapes the workspace."}, status_code=400)
    try:
        await run_in_threadpool(
            workspace_config.set_semantic_model_disabled, workspace, key, disabled
        )
    except tomllib.TOMLDecodeError:
        return JSONResponse(
            {"error": "mooring.toml is malformed — fix it before changing AI settings."},
            status_code=409,
        )
    telemetry.log_event("ai_model_toggle", disabled=int(disabled))
    return JSONResponse({"ok": True, "model": key, "ai_model_disabled": disabled})
