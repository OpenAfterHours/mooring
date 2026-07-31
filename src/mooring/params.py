"""The parameter model behind a parameterised run: the value list, the run channel, and
the artifact naming.

A month-end pack is usually one notebook run once per region, per entity, or per month.
:func:`parse_spec` turns the ``--for region=EMEA,APAC,AMER`` shorthand into a checked list
of values; :meth:`ParamSpec.env_for` turns one value into the environment the notebook's
kernel reads it from; :meth:`ParamSpec.variant` turns it into the filename fragment that
tells EMEA's artifact from APAC's.

**Why the environment, and not ``mo.cli_args()``.** ``marimo export html`` does accept
trailing notebook args, so ``mo.cli_args()`` was a real candidate. It was rejected for one
reason: mooring runs the export through TWO different launch backends (``uv run --project
… marimo`` and, frozen, ``python -m marimo`` — see :func:`mooring.editor._launch_prefix`),
so a passthrough argument has to survive an argument parser mooring does not own before it
reaches the notebook. If it ever fails to, the notebook silently runs with NO parameter and
the fan-out writes ``board-region-APAC-….html`` containing EMEA's numbers. A *mislabelled*
artifact is the worst failure this feature can have — strictly worse than not running — and
an environment variable cannot fail that way: it either arrives or it does not, identically
on both backends. Injecting ``mooring_params`` beside ``mooring_checks`` /
``mooring_inputs`` / ``mooring_connections`` then costs no new machinery at all: the same
``.mooring/pylib`` directory is already on the kernel's import path.

**The notebook runs unchanged when nothing is passed.** ``mooring_params.get(name,
default)`` takes a REQUIRED default and returns it when the environment is absent, so the
same file opens in the editor, verifies, and refreshes on a cadence exactly as before. That
is a property of the API's shape, not a convention.

This is a lean-core leaf: it imports :mod:`mooring.paths` and the standard library only, so
it carries no path to marimo, the Copilot SDK, or spaCy, and nothing here reaches the AI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mooring.paths import safe_write_bytes

STATE_DIR = ".mooring"
PYLIB_DIRNAME = "pylib"

# The packaged payload (this file's sibling) and the importable name it is written out as
# in the notebook kernel — the same two-file idiom as checks.py / inputs.py.
_RUNTIME_SRC = "_params_runtime.py"
_MODULE_NAME = "mooring_params.py"

# The one channel a value travels on. Kept in sync with _params_runtime._ENV_VAR (a test
# pins the pair — the injected module is standalone and cannot import this one).
ENV_VAR = "MOORING_PARAMS"

# A fan-out is SEQUENTIAL and each value re-runs a whole notebook, so a big list is minutes
# per value. Cap it: `--for n=1..10000` is a typo, not a request, and finding that out after
# the first hour is not acceptable.
MAX_VALUES = 50

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_INT_RE = re.compile(r"^-?\d{1,9}$")
_SLUG_MAX = 40


class ParamError(ValueError):
    """The ``--for`` spec cannot be used. ``str(exc)`` is the user-facing reason."""


@dataclass(frozen=True)
class ParamSpec:
    """One parameter and the values to run it for, already checked and de-collided."""

    name: str
    values: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.values)

    def env_for(self, value: str) -> dict[str, str]:
        """The environment overlay that hands ``value`` to the notebook's kernel."""
        return {ENV_VAR: json.dumps({self.name: value}, ensure_ascii=False)}

    def variant(self, value: str) -> str:
        """The filename fragment for ``value`` — ``region-EMEA``, so the artifact reads
        ``board-region-EMEA-20260731.html`` and a stakeholder can tell it from APAC's."""
        return f"{slug(self.name)}-{slug(value)}"

    def note(self, value: str, index: int, total: int) -> str:
        """The provenance clause stamped into ``value``'s artifact.

        It names the value AND its position, so a PARTIAL fan-out is visible from a single
        artifact: holding ``board-region-EMEA`` stamped *"value 1 of 3"* with no AMER file
        beside it, a stakeholder can see one is missing without access to mooring. This is
        the same trick the scheduled refresh uses to make staleness travel with the output.
        """
        return f"{self.name} = {value} · value {index} of {total} in this run"

    def describe(self) -> str:
        return f"{self.name} = {', '.join(self.values)}"


def parse_spec(text: str) -> ParamSpec:
    """Parse ``NAME=V1,V2,...`` (items may be ``A..B`` ranges) into a :class:`ParamSpec`.

    Ranges are a deliberately CLOSED vocabulary — integers (``1..12``) and calendar months
    (``2026-01..2026-06``) — for the same reason the schedule cadences are: the audience
    does not write a date-range grammar, and a closed set is one mooring can expand without
    guessing a step. Anything else is a literal value, so ``--for entity=ACME,Globex`` needs
    no syntax at all.

    Raises :class:`ParamError` with a user-facing reason."""
    raw = str(text or "").strip()
    if "=" not in raw:
        raise ParamError("Give the parameter as NAME=VALUES, e.g. --for region=EMEA,APAC.")
    name, _, rest = raw.partition("=")
    name = name.strip()
    if not _NAME_RE.match(name):
        raise ParamError(
            f"{name!r} is not a usable parameter name — use letters, digits and "
            "underscores, starting with a letter (e.g. region, month, entity)."
        )
    values: list[str] = []
    for item in rest.split(","):
        item = item.strip()
        if not item:
            continue
        values.extend(_expand(item))
        if len(values) > MAX_VALUES:
            raise ParamError(
                f"That is more than {MAX_VALUES} values. A run executes the whole notebook "
                "once per value, one at a time — narrow the list."
            )
    if not values:
        raise ParamError(f"No values given for {name!r} — e.g. --for {name}=EMEA,APAC.")
    _refuse_collisions(values)
    return ParamSpec(name=name, values=tuple(values))


def _expand(item: str) -> list[str]:
    lo, sep, hi = item.partition("..")
    if not sep:
        return [item]
    lo, hi = lo.strip(), hi.strip()
    months = _month_range(lo, hi)
    if months is not None:
        return months
    ints = _int_range(lo, hi)
    if ints is not None:
        return ints
    raise ParamError(
        f"{item!r} is not a range mooring can expand. Ranges are whole numbers (1..12) or "
        "calendar months (2026-01..2026-06); anything else, list the values separated by "
        "commas."
    )


def _month_range(lo: str, hi: str) -> list[str] | None:
    start, end = _MONTH_RE.match(lo), _MONTH_RE.match(hi)
    if not (start and end):
        return None
    first = int(start.group(1)) * 12 + int(start.group(2)) - 1
    last = int(end.group(1)) * 12 + int(end.group(2)) - 1
    if last < first:
        raise ParamError(f"{lo}..{hi} runs backwards — put the earlier month first.")
    return [f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(first, last + 1)]


def _int_range(lo: str, hi: str) -> list[str] | None:
    if not (_INT_RE.match(lo) and _INT_RE.match(hi)):
        return None
    first, last = int(lo), int(hi)
    if last < first:
        raise ParamError(f"{lo}..{hi} runs backwards — put the smaller number first.")
    return [str(n) for n in range(first, last + 1)]


def _refuse_collisions(values: list[str]) -> None:
    """Refuse any two values whose ARTIFACT NAMES could not be told apart.

    Windows filesystems are case-insensitive, so ``EMEA`` and ``emea`` would land on one
    file and the second run would silently overwrite the first — one artifact, two claims.
    Refusing up front is the only honest answer; renaming behind the user's back would put
    a value in a filename they never typed."""
    seen: dict[str, str] = {}
    for value in values:
        key = slug(value).casefold()
        if not key:
            raise ParamError(
                f"{value!r} has no letters or digits, so it cannot name an artifact — "
                "use values like EMEA, 2026-01 or ACME."
            )
        if key in seen:
            other = seen[key]
            same = "twice" if other == value else f"and {other!r} cannot be told apart"
            raise ParamError(
                f"{value!r} appears {same} once written into a filename — every value must "
                "produce a distinct artifact name."
            )
        seen[key] = value


def slug(value: str) -> str:
    """A filesystem-safe fragment of ``value``: keep ``A-Za-z0-9._-``, collapse the rest to
    ``-``, and bound the length so a long value cannot blow the path limit."""
    cleaned = _UNSAFE.sub("-", str(value)).strip("-.")
    return cleaned[:_SLUG_MAX].strip("-.")


def reads_parameter(source: str, name: str) -> bool:
    """Whether ``source`` looks like a notebook that reads the parameter ``name``.

    A cheap, deliberately literal check: the source must mention ``mooring_params`` AND the
    parameter's name as a quoted string. It exists to catch the one failure this feature
    cannot recover from — running a notebook that ignores the parameter N times and writing
    N differently-named artifacts with IDENTICAL contents. A typo (``--for regoin=…``) is
    exactly that failure, and it is otherwise invisible until a stakeholder acts on the
    wrong number."""
    if "mooring_params" not in source:
        return False
    return f'"{name}"' in source or f"'{name}'" in source


# -- the injected kernel module ----------------------------------------------


def pylib_dir(workspace: Path | str) -> Path:
    """The directory on the notebook kernel's import path (shared with the checks,
    inputs and connections runtimes)."""
    return Path(workspace) / STATE_DIR / PYLIB_DIRNAME


def _payload_source() -> bytes:
    return Path(__file__).with_name(_RUNTIME_SRC).read_bytes()


def install_runtime(workspace: Path | str) -> None:
    """Write the payload to ``<ws>/.mooring/pylib/mooring_params.py``.

    Best-effort and idempotent (only rewrites when the bytes differ), and never raises — a
    failure just means ``import mooring_params`` surfaces a clear ImportError in the
    analyst's own cell rather than breaking the editor. Same contract as
    :func:`mooring.checks.install_runtime`."""
    try:
        src = _payload_source()
    except OSError:
        return
    target = pylib_dir(workspace) / _MODULE_NAME
    try:
        if target.is_file() and target.read_bytes() == src:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_write_bytes(target, src)
    except OSError:
        pass
