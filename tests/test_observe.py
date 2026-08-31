"""The eye: what a cell was SUPPOSED to define, and what the kernel actually has.

Two halves, and the risky one is the second:

* ``marimo_rt.cell_defs`` reads each cell's definitions out of marimo's dataflow
  graph. It must be COMPILE-only — the source it is handed is model-authored and
  has not been reviewed by anybody, so a version that executed the notebook would
  be a remote-code-execution hole dressed up as static analysis.
* ``introspect.observe`` waits for the live kernel to settle after a change and
  reports which expected names are bound. Its one unforgivable failure is
  reporting "your cell did not run" about a cell that ran fine, so these tests pin
  the asymmetry: names present is enough to settle, names ABSENT settles only with
  a still namespace, an idle kernel and the settle floor passed — and a timeout is
  always ``observed=False`` with an EMPTY ``missing``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from mooring import marimo_rt
from mooring.ai import introspect
from mooring.schema import DatasetSchema

SECRET = "SECRET_VALUE_DO_NOT_LEAK"

HEAD = 'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App()\n\n\n'
TAIL = 'if __name__ == "__main__":\n    app.run()\n'


def nb(*cells: str) -> str:
    """A marimo notebook whose cells have the given bodies (marimo's own file shape)."""
    parts = [HEAD]
    for code in cells:
        body = "\n".join("    " + line for line in code.splitlines())
        parts.append(f"@app.cell\ndef _():\n{body}\n    return\n\n\n")
    parts.append(TAIL)
    return "".join(parts)


# --- cell_defs -------------------------------------------------------------


def test_cell_defs_reports_what_each_cell_defines():
    source = nb(
        "import polars as pl",
        "df = pl.DataFrame({'a': [1]})\nn_rows = df.height",
        'mo.md(r"""# a heading""")',  # markdown cell: defines nothing
        "def helper(x):\n    return x + 1",
    )
    assert marimo_rt.cell_defs(source) == [
        (0, ("pl",)),
        (1, ("df", "n_rows")),
        (2, ()),
        (3, ("helper",)),
    ]


def test_cell_defs_indices_match_read_cells():
    # The whole point of the index: a caller that just wrote cell N asks cell_defs
    # what cell N was supposed to bind. A different numbering would answer about
    # the wrong cell, silently.
    source = nb("a = 1", 'mo.md("x")', "b = a + 1", "c, d = 1, 2")
    assert [i for i, _ in marimo_rt.cell_defs(source)] == [i for i, _ in marimo_rt.read_cells(source)]
    assert dict(marimo_rt.cell_defs(source))[3] == ("c", "d")


def test_cell_defs_never_executes_the_notebook(tmp_path):
    # If cell_defs ran the notebook, this cell would create the sentinel and then
    # raise ZeroDivisionError. It must do neither, and still report both names.
    sentinel = tmp_path / "EXECUTED"
    source = nb(
        f"from pathlib import Path\nPath(r{str(sentinel)!r}).write_text('ran')\nboom = 1 / 0",
        "import os\nos.environ['MOORING_CELL_DEFS_SIDE_EFFECT'] = '1'",
    )
    defs = marimo_rt.cell_defs(source)

    assert not sentinel.exists(), "cell_defs executed the notebook"
    import os

    assert "MOORING_CELL_DEFS_SIDE_EFFECT" not in os.environ
    assert defs == [(0, ("Path", "boom")), (1, ("os",))]


def test_cell_defs_fails_soft_on_things_it_cannot_read():
    assert marimo_rt.cell_defs("this is not a notebook at all") == []
    assert marimo_rt.cell_defs("") == []
    assert marimo_rt.cell_defs(nb("def broken(:\n    pass")) == []  # a cell that will not compile
    assert marimo_rt.cell_defs("x" * (marimo_rt.VALIDATE_MAX_BYTES + 1)) == []
    over = nb(*[f"v{i} = {i}" for i in range(marimo_rt.VALIDATE_MAX_CELLS + 1)])
    assert marimo_rt.cell_defs(over) == []


def test_cell_defs_degrades_when_marimo_internals_move(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("the private API moved")

    monkeypatch.setattr(marimo_rt, "_codegen_api", boom)
    assert marimo_rt.cell_defs(nb("a = 1")) == []


# --- observe: the fake kernel ----------------------------------------------


def _probe_args(code: str):
    """The ``(sidecar_path, asked_names)`` the frozen probe was built with."""
    call = [ln for ln in code.splitlines() if ln.startswith("_mooring_probe(")][-1]
    return ast.literal_eval(call[len("_mooring_probe") :])


class FakeEditor:
    running = True
    port = 1234
    token = "tok"


class FakeKernel:
    """Stands in for marimo_rt.KernelControl: each ``run`` writes the next scripted
    readback to the sidecar the probe named (``None`` writes nothing at all, which
    is what a probe queued behind a running cascade really looks like)."""

    def __init__(self, readings, *, state="idle", session="sid-1"):
        self.readings = list(readings)
        self.state = state
        self.session = session
        self.asked: list[tuple[str, ...]] = []
        self.runs = 0

    def __call__(self, port, token, *, timeout=None):  # constructed by observe()
        return self

    def session_for(self, notebook_rel):
        return self.session

    def run(self, session_id, code, *, cell_id="mooring-introspect"):
        path, names = _probe_args(code)
        self.asked.append(tuple(names))
        # cycles, so a script of DISTINCT readings never settles however long we look
        data = self.readings[self.runs % len(self.readings)] if self.readings else None
        self.runs += 1
        if data is not None:
            Path(path).write_text(json.dumps(data), encoding="utf-8")

    def kernel_state(self, session_id):
        return self.state


def reading(*names, frames=(), **types):
    """A probe readback: ``reading('a', b='DataFrame')`` -> a absent, b present."""
    entries = [{"name": n, "present": False, "type": None} for n in names]
    entries += [{"name": n, "present": True, "type": t} for n, t in types.items()]
    return {"frames": list(frames), "names": entries}


@pytest.fixture
def fast(monkeypatch):
    """Shrink the real-time constants so the loop is testable in milliseconds."""
    monkeypatch.setattr(introspect, "SETTLE_FLOOR_SECONDS", 0.0)
    monkeypatch.setattr(introspect, "_OBSERVE_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(introspect, "_OBSERVE_READ_SLICE", 0.2)


def install(monkeypatch, kernel):
    monkeypatch.setattr(marimo_rt, "KernelControl", kernel)
    return kernel


# --- observe: the settle rule ----------------------------------------------


def test_observe_settles_as_soon_as_every_expected_name_is_bound(monkeypatch, fast):
    frame = {"name": "sales", "columns": [["region", "String"]], "n_rows": 12}
    kernel = install(monkeypatch, FakeKernel([reading(frames=[frame], sales="DataFrame")]))

    obs = introspect.observe(FakeEditor(), "nb.py", ["sales"], timeout=5.0)

    assert obs.observed is True
    assert obs.present == ("sales",) and obs.missing == ()
    assert obs.types == (("sales", "DataFrame"),)
    assert [f.name for f in obs.frames] == ["sales"]
    assert kernel.runs == 1, "a bound name is final — no need for a second look"


def test_observe_reports_a_missing_name_once_the_kernel_is_still_and_idle(monkeypatch, fast):
    still = reading("totals", sales="DataFrame")
    kernel = install(monkeypatch, FakeKernel([still, still, still], state="idle"))

    obs = introspect.observe(FakeEditor(), "nb.py", ["sales", "totals"], timeout=5.0)

    assert obs.observed is True
    assert obs.missing == ("totals",)
    assert obs.present == ("sales",)
    assert kernel.runs >= 2, "one readback can never settle a MISSING verdict"


def test_a_timeout_is_never_a_failure_report(monkeypatch, fast):
    # The namespace keeps changing, so it never settles. The honest answer is
    # "could not observe" — reporting `totals` as missing here would send the model
    # to repair a cell that may well be mid-run.
    kernel = install(
        monkeypatch,
        FakeKernel(
            [reading("totals"), reading("totals", other="int"), reading("totals", third="int")]
        ),
    )

    obs = introspect.observe(FakeEditor(), "nb.py", ["totals"], timeout=0.5)

    assert obs.observed is False
    assert obs.missing == (), "a timeout must NEVER come back as missing"
    assert "did not settle" in obs.detail
    assert kernel.runs >= 2


def test_the_settle_floor_holds_a_missing_verdict_back(monkeypatch, fast):
    # Same still, idle kernel — but the floor has not passed, which is the window in
    # which marimo's file watcher may simply not have noticed the write yet. The floor
    # is NOT shortened to fit the budget: a caller in a hurry does not get a cheaper
    # standard of proof, it gets "could not observe".
    monkeypatch.setattr(introspect, "SETTLE_FLOOR_SECONDS", 30.0)
    still = reading("totals")
    install(monkeypatch, FakeKernel([still, still, still, still], state="idle"))

    obs = introspect.observe(FakeEditor(), "nb.py", ["totals"], timeout=0.4)

    assert obs.observed is False and obs.missing == ()


def test_a_name_that_is_bound_still_settles_inside_the_floor(monkeypatch, fast):
    # The floor only ever delays a MISSING verdict. A bound name is final the moment
    # it is seen, so the common case stays fast even with a long floor.
    monkeypatch.setattr(introspect, "SETTLE_FLOOR_SECONDS", 30.0)
    install(monkeypatch, FakeKernel([reading(sales="DataFrame")]))

    obs = introspect.observe(FakeEditor(), "nb.py", ["sales"], timeout=0.5)

    assert obs.observed is True and obs.present == ("sales",)


def test_a_busy_kernel_never_settles_a_missing_verdict(monkeypatch, fast):
    still = reading("totals")
    install(monkeypatch, FakeKernel([still, still, still, still], state="running"))

    obs = introspect.observe(FakeEditor(), "nb.py", ["totals"], timeout=0.4)

    assert obs.observed is False and obs.missing == ()


def test_an_unknown_kernel_state_blocks_a_missing_verdict(monkeypatch, fast):
    # A marimo without the status endpoint (or a transport hiccup) means no busy
    # signal at all; the verdict must fail closed to "could not observe".
    still = reading("totals")

    class NoStatus(FakeKernel):
        def kernel_state(self, session_id):
            raise marimo_rt.MarimoTransportError("no status endpoint")

    install(monkeypatch, NoStatus([still, still, still, still]))
    obs = introspect.observe(FakeEditor(), "nb.py", ["totals"], timeout=0.4)

    assert obs.observed is False and obs.missing == ()


def test_a_probe_that_never_writes_back_times_out_rather_than_lying(monkeypatch, fast):
    # What a probe queued behind a long-running cascade actually looks like: no
    # sidecar at all. Nothing may be concluded from silence.
    install(monkeypatch, FakeKernel([None]))

    obs = introspect.observe(FakeEditor(), "nb.py", ["totals"], timeout=0.4)

    assert obs.observed is False and obs.missing == () and obs.present == ()


def test_observe_asks_only_about_askable_names(monkeypatch, fast):
    kernel = install(monkeypatch, FakeKernel([reading(frames=[], good="int")]))

    introspect.observe(
        FakeEditor(), "nb.py", ["good", "_cell_local", "not an identifier", 7, "good"], timeout=2.0
    )

    assert kernel.asked[0] == ("good",)


def test_a_single_name_passed_bare_is_not_read_letter_by_letter(monkeypatch, fast):
    kernel = install(monkeypatch, FakeKernel([reading(frames=[], sales="DataFrame")]))

    introspect.observe(FakeEditor(), "nb.py", "sales", timeout=2.0)

    assert kernel.asked[0] == ("sales",)


def test_observe_with_nothing_to_expect_settles_on_the_first_readback(monkeypatch, fast):
    frame = {"name": "df", "columns": [["a", "Int64"]], "n_rows": 1}
    install(monkeypatch, FakeKernel([{"frames": [frame], "names": []}]))

    obs = introspect.observe(FakeEditor(), "nb.py", [], timeout=2.0)

    assert obs.observed is True and obs.missing == ()
    assert [f.name for f in obs.frames] == ["df"]


def test_observe_degrades_when_there_is_nothing_to_look_at(monkeypatch, fast):
    assert introspect.observe(None, "nb.py", ["x"]).observed is False
    assert "editor is not running" in introspect.observe(None, "nb.py", ["x"]).detail

    install(monkeypatch, FakeKernel([], session=None))
    obs = introspect.observe(FakeEditor(), "nb.py", ["x"], timeout=1.0)
    assert obs.observed is False and "no running kernel session" in obs.detail


def test_observe_survives_a_transport_failure(monkeypatch, fast):
    class Dead(FakeKernel):
        def run(self, *a, **k):
            raise marimo_rt.MarimoTransportError("connection refused")

    install(monkeypatch, Dead([]))
    obs = introspect.observe(FakeEditor(), "nb.py", ["x"], timeout=1.0)

    assert obs.observed is False and obs.missing == ()
    assert "could not be reached" in obs.detail


def test_observe_never_raises_even_if_the_loop_breaks(monkeypatch, fast):
    class Exploding(FakeKernel):
        def run(self, *a, **k):
            raise RuntimeError("something nobody predicted")

    install(monkeypatch, Exploding([]))
    obs = introspect.observe(FakeEditor(), "nb.py", ["x"], timeout=1.0)

    assert obs.observed is False and obs.detail == "the observation failed"


def test_observe_leaves_no_sidecar_files_behind(monkeypatch, fast, tmp_path):
    monkeypatch.setattr(introspect.tempfile, "gettempdir", lambda: str(tmp_path))
    still = reading("totals")
    install(monkeypatch, FakeKernel([still, still, still], state="idle"))

    introspect.observe(FakeEditor(), "nb.py", ["totals"], timeout=2.0)

    assert list(tmp_path.iterdir()) == []


# --- format_observation ----------------------------------------------------


def test_format_observation_renders_schema_and_states_the_missing_name():
    obs = introspect.Observation(
        frames=(
            DatasetSchema(name="sales", columns=(("region", "String"), ("amount", "Int64")), n_rows=1500),
            DatasetSchema(name="lookup", columns=(("k", "String"),), n_rows=None),
        ),
        present=("sales", "n_total"),
        missing=("totals",),
        types=(("sales", "DataFrame"), ("n_total", "int")),
        observed=True,
    )
    text = introspect.format_observation(obs)

    assert "`sales` is bound (DataFrame, 1,500 rows):" in text
    assert "- region: String" in text
    assert "`n_total` is bound (int)." in text
    assert "NOT bound in the kernel: `totals`." in text
    assert "did not run to completion" in text
    assert "Also loaded in this session: `lookup` (1 column)." in text
    # fact, not diagnosis: no cause is invented for the missing name
    assert "error" not in text.lower() and "failed" not in text.lower()


def test_format_observation_says_plainly_that_it_could_not_see():
    text = introspect.format_observation(
        introspect.Observation(detail="the notebook's kernel did not settle within 20s")
    )
    assert "could not observe" in text
    assert "NOT a report that anything failed" in text
    assert "did not run to completion" not in text


def test_format_observation_carries_no_values():
    # The one field that could carry one is the type name; it is a class name.
    obs = introspect.Observation(
        frames=(DatasetSchema(name="df", columns=((SECRET, "String"),), n_rows=3),),
        present=("df",),
        types=(("df", "DataFrame"),),
        observed=True,
    )
    text = introspect.format_observation(obs)
    assert "DataFrame" in text
    # a column NAME is allowed out (that is the schema); nothing else about the frame is
    assert text.count(SECRET) == 1
