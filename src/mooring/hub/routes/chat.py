"""AI copilot chat endpoints: open/stream/send/apply/rollback, the value-free
dataset+model listings, Copilot sign-in, and the per-notebook AI toggle."""

from __future__ import annotations

import asyncio
import secrets
import tomllib
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mooring import telemetry, workspace_config
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
    reasoning_effort = (
        str(data.get("reasoning_effort", "")).strip() or hub.app_cfg.ai_reasoning_effort
    )
    if not notebook:
        return JSONResponse({"error": "A notebook is required."}, status_code=400)
    workspace = hub.cfg.workspace()
    # Per-notebook opt-out (synced mooring.toml). 403 + reason distinguishes
    # this from the global-off 404 above, so the chat UI shows the right message.
    if workspace_config.is_ai_disabled(workspace, notebook):
        return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
    try:
        # File IO (notebook source, dataset schema, team context, semantic-model
        # extraction) — off the event loop so a slow read can't stall the hub's
        # other requests.
        context, index, pii_banner, live_text, models = await run_in_threadpool(
            hub._build_chat_context, workspace, notebook, dataset
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": f"No such file: {exc}"}, status_code=404)
    hub._reap_idle_chats()
    try:
        session = hub._make_chat_session(
            context,
            workspace,
            notebook,
            model=model,
            reasoning_effort=reasoning_effort,
            dictionary=index,
            semantic_models=models,
        )
    except Exception as exc:  # noqa: BLE001  # AIError surfaces to the UI in Phase 1
        return JSONResponse({"error": str(exc)}, status_code=502)
    # The live-kernel schema is deferred off the open path (see _build_chat_context),
    # so live_text is ""; the first turn picks it up. This seeds the (empty) snapshot.
    session.set_initial_live_schema(live_text)
    # Kick off the (one-time) NER model download in the background with progress,
    # so name detection doesn't hang the first chat turn silently.
    session.prepare_pii_model()
    sid = secrets.token_urlsafe(9)
    hub.chat.register(sid, session, workspace, notebook)
    telemetry.log_event("ai_chat_open")
    if pii_banner:  # count only — never a kind/value reaches the central sink
        telemetry.log_event("ai_pii", findings=len(pii_banner))
    return JSONResponse(
        {
            "sid": sid,
            "notebook": notebook,
            "pii": pii_banner,
            "guard": hub._pii_status(),
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
    workspace_str, notebook_rel = target
    workspace = Path(workspace_str)
    from mooring.ai.cellwrite import CellApplyConflict, CellWriteError

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
            hub.apply.apply_with_undo, nb_path, workspace, notebook_rel, op_dicts
        )
    except PermissionError:  # disabled between the gate above and the write
        hub.chat.close(sid)
        return JSONResponse({"enabled": False, "reason": "notebook_disabled"}, status_code=403)
    except CellApplyConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except CellWriteError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    telemetry.log_event("ai_chat_apply")
    hub._activity("ai_apply", path=notebook_rel)
    return JSONResponse({"ok": True, "can_undo": undo_depth > 0, "undo_depth": undo_depth})


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


async def api_chat_models(request: Request) -> JSONResponse:
    """The models the user can pick, plus the configured defaults (value-free)."""
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"enabled": False}, status_code=404)
    provider = hub._provider_for()
    models = await asyncio.to_thread(provider.list_models)
    payload = {
        "models": models,
        "default_model": hub.app_cfg.ai_model or "",
        "default_effort": hub.app_cfg.ai_reasoning_effort or "",
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
