"""The Settings page's API: read, write (allowlisted + confirm-gated), reset.

The editable surface is the curated registry in settings_schema.py — which IS
the allowlist the rest of the config write path lacks (config_store.set_value
writes any key verbatim). Writes mirror api_set_theme: persist via
config_store, then re-read the config in place (NOT the destructive reload()).
Team/synced decisions (the per-notebook AI opt-out, sync folders) and the
structural value-blindness guarantees are intentionally NOT here. See
docs/admins/configuration.md.

One team decision DOES reach this page, because hiding it would be dishonest: a
setting pinned by the repo's admin policy (``[policy.settings]`` in the synced
mooring.toml — see mooring.policy) is returned with ``locked: true`` and refuses
a write with a 409 naming where the lock came from. It is NOT silently ignored,
and it is NOT merely cosmetic: ``Hub.app_cfg`` is tightened at every assignment,
so the effective config obeys the policy even if config.toml disagrees.
"""

from __future__ import annotations

import tomllib

from starlette.requests import Request
from starlette.responses import JSONResponse

from mooring import config_store, telemetry
from mooring.hub import settings_schema


def _policy_refusal(hub, key: str, value: object) -> JSONResponse | None:
    """409 when the team's policy pins ``key`` to a different value than ``value``.

    Checked BEFORE the weakening-confirm: a locked setting has no confirm path at
    all — no dialog can talk a user past a policy. Writing the pinned value itself
    is allowed through (it is a no-op that leaves config.toml agreeing with the
    policy), so a user is never blocked from moving TOWARDS the safe direction.
    """
    locked = hub._policy_lock(key)
    if locked is None or value == locked:
        return None
    return JSONResponse(
        {
            "error": (
                f"“{settings_schema.by_key(key).label}” is set by your team's policy "
                "and can't be changed here."
            ),
            "locked": True,
            "key": key,
            "locked_value": locked,
            "message": settings_schema.POLICY_LOCK_NOTE,
        },
        status_code=409,
    )


def api_get_settings(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.hub._settings_payload())


async def api_set_settings(request: Request) -> JSONResponse:
    """Persist one per-machine setting and make it live. The allowlist is the
    registry: a key with no SettingSpec is a 400, so this can never write the
    dead/unread keys a raw `mooring config set` could. A privacy-weakening flip
    needs an explicit confirm (409 needs_confirm otherwise)."""
    hub = request.app.state.hub
    data = await request.json()
    key = str(data.get("key", ""))
    spec = settings_schema.by_key(key)
    if spec is None:
        return JSONResponse({"error": f"Unknown or read-only setting {key!r}."}, status_code=400)
    if "value" not in data:
        return JSONResponse({"error": "A value is required."}, status_code=400)
    try:
        value = settings_schema.coerce(spec, data["value"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        hub._validate_routing_default_setting(key, value)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001 - do not expose managed endpoint/key detail
        return JSONResponse(
            {"error": "The approved customer-data profile is unavailable."},
            status_code=409,
        )
    refusal = _policy_refusal(hub, key, value)
    if refusal is not None:
        return refusal
    if hub._needs_confirm(spec, value) and not bool(data.get("confirm")):
        return JSONResponse(
            {"needs_confirm": True, "key": key, "message": spec.confirm}, status_code=409
        )
    try:
        config_store.set_value(key, value)
    except tomllib.TOMLDecodeError:
        return JSONResponse(
            {"error": "Your config.toml is malformed — fix it before changing settings."},
            status_code=409,
        )
    except OSError as exc:
        return JSONResponse({"error": f"Could not save the setting: {exc}"}, status_code=502)
    hub._apply_setting_change()
    # Value-free telemetry: the key plus, for non-text settings, the new
    # boolean/number/enum — never a model id, label, or path.
    extra = {"value": value} if spec.type in ("bool", "int", "float", "enum") else {}
    telemetry.log_event("settings_change", key=key, **extra)
    return JSONResponse({"ok": True, **hub._settings_payload()})


async def api_reset_settings(request: Request) -> JSONResponse:
    """Revert one setting to the packaged default (delete it from config.toml)."""
    hub = request.app.state.hub
    data = await request.json()
    key = str(data.get("key", ""))
    spec = settings_schema.by_key(key)
    if spec is None:
        return JSONResponse({"error": f"Unknown or read-only setting {key!r}."}, status_code=400)
    # Reset is a WRITE to the packaged default, so it can land on the weakening
    # value (e.g. ai.pii.enabled reverts to its off default) — gate it the same as
    # a set, policy first, rather than letting Reset slip past either check.
    refusal = _policy_refusal(hub, key, spec.default)
    if refusal is not None:
        return refusal
    if hub._needs_confirm(spec, spec.default) and not bool(data.get("confirm")):
        return JSONResponse(
            {"needs_confirm": True, "key": key, "message": spec.confirm}, status_code=409
        )
    try:
        config_store.unset_value(key)
    except tomllib.TOMLDecodeError:
        return JSONResponse(
            {"error": "Your config.toml is malformed — fix it before changing settings."},
            status_code=409,
        )
    except OSError as exc:
        return JSONResponse({"error": f"Could not save the setting: {exc}"}, status_code=502)
    hub._apply_setting_change()
    telemetry.log_event("settings_reset", key=key)
    return JSONResponse({"ok": True, **hub._settings_payload()})
