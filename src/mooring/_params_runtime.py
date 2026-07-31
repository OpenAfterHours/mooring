"""mooring_params — the parameter value the current run was given.

mooring INJECTS this module into ``<workspace>/.mooring/pylib/mooring_params.py`` and puts
that directory on the marimo kernel's import path (see
:func:`mooring.editor.ensure_runtime_config`), so a notebook can be run once per region,
entity or month without editing it in between::

    import mooring_params

    region = mooring_params.get("region", "EMEA")   # the default is REQUIRED
    sales = load(f"sales_{region}.parquet")

``mooring run notebooks/board.py --for region=EMEA,APAC,AMER`` then runs the notebook once
per value and writes one artifact per value.

**The default is required, and that is the whole safety property.** With nothing passed —
opening the notebook in the marimo editor, ``mooring verify``, a scheduled refresh, or
plain ``marimo run`` — :func:`get` returns the default, so the notebook behaves exactly as
it did before it was parameterised. There is deliberately no way to write a cell that only
works inside a fan-out: ``get("region")`` is a TypeError at the call site, not a ``None``
that quietly becomes the string ``"None"`` in a filename.

The return type always matches the default's type, so ``get("month", 1)`` is an ``int``
whether or not a value was passed, and a value that will not convert raises immediately
rather than flowing on as a string.

Standalone by design: imports only the standard library, so it works in the team's locked
uv environment and in the frozen bundle alike. Do not import mooring here.
"""

from __future__ import annotations

import json
import os

# The one channel. Kept in sync with mooring.params.ENV_VAR (a test pins the pair — this
# module is standalone and cannot import mooring).
_ENV_VAR = "MOORING_PARAMS"

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def as_dict() -> dict:
    """Every parameter this run was given, as ``{name: raw string}``.

    Empty when the notebook is being run normally. Malformed content is treated as no
    parameters at all rather than raising — a broken channel must not stop a notebook that
    has perfectly good defaults."""
    raw = os.environ.get(_ENV_VAR, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def names() -> list:
    """The parameter names this run was given, sorted. Empty for a normal run."""
    return sorted(as_dict())


def is_parameterised() -> bool:
    """Whether this run was given any parameter at all.

    Useful for a title cell — ``mo.md(f"# Board — {region}")`` reads fine either way, but a
    notebook that wants to say "all regions" when unparameterised can ask."""
    return bool(as_dict())


def get(name: str, default):
    """The value of parameter ``name``, or ``default`` when this run was not given one.

    ``default`` is required. The result has the same TYPE as ``default`` for ``bool``,
    ``int`` and ``float`` defaults (so arithmetic on it is safe), and is the raw string
    otherwise. A value that cannot be converted raises ``ValueError`` — loudly, in the
    analyst's own cell — rather than being silently ignored, because a run that quietly
    fell back to the default would produce an artifact labelled with a value it never
    used."""
    raw = as_dict().get(str(name))
    if raw is None:
        return default
    if isinstance(default, bool):  # checked first: bool is a subclass of int
        return _as_bool(name, raw)
    if isinstance(default, int):
        return _convert(name, raw, int, "a whole number")
    if isinstance(default, float):
        return _convert(name, raw, float, "a number")
    return raw


def _as_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(
        f"mooring_params: {name}={raw!r} is not a true/false value "
        "(use true/false, yes/no, 1/0)."
    )


def _convert(name: str, raw: str, kind, described: str):
    try:
        return kind(raw.strip())
    except (TypeError, ValueError):
        raise ValueError(f"mooring_params: {name}={raw!r} is not {described}.") from None
