"""The hub's HTML page endpoints (index, chat, batch, settings).

Every page is served with the configured default theme inlined: the page's
pre-paint script applies ``localStorage`` first, falling back to this server
default — so a brand-new browser (empty localStorage) paints in the admin's
configured theme immediately, with no flash, and consistent with what
``/api/state`` later reports.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse


def _themed(hub, filename: str) -> HTMLResponse:
    """The theme value comes from :func:`mooring.config.normalize_theme`, so it
    is always one of light/dark/system (no injection risk)."""
    from mooring.hub.server import _static_dir

    html = (_static_dir() / filename).read_text("utf-8")
    return HTMLResponse(html.replace("__MOORING_DEFAULT_THEME__", hub.app_cfg.ui_theme))


def index_page(request: Request) -> HTMLResponse:
    return _themed(request.app.state.hub, "index.html")


def chat_page(request: Request) -> HTMLResponse | JSONResponse:
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"error": "The AI copilot is disabled."}, status_code=404)
    return _themed(hub, "chat.html")


def batch_page(request: Request) -> HTMLResponse | JSONResponse:
    hub = request.app.state.hub
    if not hub.app_cfg.ai_enabled:
        return JSONResponse({"error": "The AI copilot is disabled."}, status_code=404)
    return _themed(hub, "batch.html")


def settings_page(request: Request) -> HTMLResponse:
    """The Settings page, served like chat/batch so it pre-paints the theme."""
    return _themed(request.app.state.hub, "settings.html")


def activity_page(request: Request) -> HTMLResponse:
    """The Activity page (the local ledger + the Trash panel), themed like the rest."""
    return _themed(request.app.state.hub, "activity.html")
