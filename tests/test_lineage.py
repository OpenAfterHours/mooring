"""The value-free lineage graph derived from the mooring_inputs receipts.

Most cases drive the REAL injected runtime (loaded from ``.mooring/pylib`` the way a
marimo kernel would, with a fake ``__file__`` cell global), so what is asserted is the
graph a genuine notebook run produces — not a hand-built fixture that could drift from it.
Hand-written receipts are used only where the point IS the file format (an old receipt,
a corrupt one).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import polars as pl

from mooring import inputs, lineage

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
RECENT = "2026-07-30T09:00:00+00:00"


def _receipt(notebook, *, updated=RECENT, reads=(), writes=()):
    """A receipt in the shape :func:`mooring.inputs.read_receipts` yields, for the cases
    where the point is the graph's own behaviour rather than the runtime's."""
    return {
        "notebook": notebook,
        "updated": updated,
        "inputs": [{"rel": rel} for rel in reads],
        "outputs": [{"rel": rel} for rel in writes],
    }


def _ws(tmp_path, *notebooks):
    ws = tmp_path / "ws"
    (ws / "notebooks").mkdir(parents=True)
    for name in notebooks or ("recon.py",):
        (ws / "notebooks" / name).write_text("# notebook\n", "utf-8")
    return ws


def _load_payload(ws):
    import importlib.util

    inputs.install_runtime(ws)
    mod_path = inputs.pylib_dir(ws) / "mooring_inputs.py"
    spec = importlib.util.spec_from_file_location("mooring_inputs_lineage_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _data(ws, rel, text="a\n1\n"):
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
    return target


def _reads(mi, ws, notebook, path, name=None):
    g = {"mi": mi, "df": pl.read_csv(ws / path), "__file__": str(ws / notebook)}
    exec(f"mi.fingerprint(df, {(name or path)!r}, path={str(ws / path)!r})", g)


def _writes(mi, ws, notebook, path, name=None):
    g = {"mi": mi, "df": pl.read_csv(ws / path), "__file__": str(ws / notebook)}
    exec(f"mi.output(df, {(name or path)!r}, path={str(ws / path)!r})", g)


def _chain(tmp_path):
    """ingest.py: raw.csv -> sales.csv · recon.py: sales.csv -> monthly.csv · board.py reads
    sales.csv. The shape every impact question in this file is asked against."""
    ws = _ws(tmp_path, "ingest.py", "recon.py", "board.py")
    mi = _load_payload(ws)
    for rel in ("data/raw.csv", "data/sales.csv", "data/monthly.csv"):
        _data(ws, rel)
    _reads(mi, ws, "notebooks/ingest.py", "data/raw.csv")
    _writes(mi, ws, "notebooks/ingest.py", "data/sales.csv")
    _reads(mi, ws, "notebooks/recon.py", "data/sales.csv")
    _writes(mi, ws, "notebooks/recon.py", "data/monthly.csv")
    _reads(mi, ws, "notebooks/board.py", "data/sales.csv")
    return ws


def test_build_records_who_reads_and_who_writes(tmp_path):
    graph = lineage.build(_chain(tmp_path))
    assert lineage.readers(graph, "data/sales.csv") == (
        "notebooks/board.py",
        "notebooks/recon.py",
    )  # sorted, not receipt-read order
    assert lineage.writers(graph, "data/sales.csv") == ("notebooks/ingest.py",)
    assert lineage.readers(graph, "data/raw.csv") == ("notebooks/ingest.py",)
    assert lineage.writers(graph, "data/raw.csv") == ()  # nothing recorded writes it
    assert set(graph.notebooks) == {
        "notebooks/ingest.py",
        "notebooks/recon.py",
        "notebooks/board.py",
    }


def test_downstream_is_the_transitive_impact_of_a_change(tmp_path):
    graph = lineage.build(_chain(tmp_path))
    impact = lineage.downstream(graph, "data/sales.csv")
    assert impact.notebooks == ("notebooks/board.py", "notebooks/recon.py")
    assert impact.datasets == ("data/monthly.csv",)  # what recon.py rewrites in turn
    # From the top of the chain the whole thing is reachable — that is the "what breaks?"
    # answer, and it must cross the notebook that sits in the middle.
    everything = lineage.downstream(graph, "data/raw.csv")
    assert everything.notebooks == (
        "notebooks/board.py",
        "notebooks/ingest.py",
        "notebooks/recon.py",
    )
    assert everything.datasets == ("data/monthly.csv", "data/sales.csv")


def test_upstream_is_what_a_file_is_built_from(tmp_path):
    graph = lineage.build(_chain(tmp_path))
    source = lineage.upstream(graph, "data/monthly.csv")
    assert source.notebooks == ("notebooks/ingest.py", "notebooks/recon.py")
    assert source.datasets == ("data/raw.csv", "data/sales.csv")
    assert lineage.upstream(graph, "data/raw.csv") == lineage.Impact()  # a true leaf


def test_a_cycle_terminates(tmp_path):
    # a.py writes x and reads y; b.py reads x and writes y. Ordinary enough to happen by
    # accident, and the closure walk must finish rather than ping-pong forever.
    ws = _ws(tmp_path, "a.py", "b.py")
    mi = _load_payload(ws)
    _data(ws, "data/x.csv")
    _data(ws, "data/y.csv")
    _writes(mi, ws, "notebooks/a.py", "data/x.csv")
    _reads(mi, ws, "notebooks/a.py", "data/y.csv")
    _reads(mi, ws, "notebooks/b.py", "data/x.csv")
    _writes(mi, ws, "notebooks/b.py", "data/y.csv")
    graph = lineage.build(ws)
    impact = lineage.downstream(graph, "data/x.csv")
    assert impact.notebooks == ("notebooks/a.py", "notebooks/b.py")
    assert impact.datasets == ("data/y.csv",)
    assert lineage.upstream(graph, "data/x.csv").notebooks == ("notebooks/a.py", "notebooks/b.py")


def test_a_notebook_that_reads_and_writes_the_same_file_terminates(tmp_path):
    # The tightest possible cycle: a self-loop on one dataset.
    ws = _ws(tmp_path, "a.py")
    mi = _load_payload(ws)
    _data(ws, "data/x.csv")
    _reads(mi, ws, "notebooks/a.py", "data/x.csv", name="in")
    _writes(mi, ws, "notebooks/a.py", "data/x.csv", name="out")
    graph = lineage.build(ws)
    impact = lineage.downstream(graph, "data/x.csv")
    assert impact.notebooks == ("notebooks/a.py",)
    assert impact.datasets == ()  # the seed is never reported as its own consequence


def test_counts_never_report_a_zero(tmp_path):
    # THE structural honesty guarantee, and the payload is where it has to live: a hub row
    # cannot render "0 notebooks read this" if it is never told a zero. `data/monthly.csv`
    # is the case that matters — a DERIVED OUTPUT, the file most likely to be consumed by
    # a dashboard or a spreadsheet mooring will never hear about, and so the file whose
    # "0 readers" would be most confidently wrong.
    graph = lineage.build(_chain(tmp_path))
    got = lineage.counts(
        graph, ["data/sales.csv", "data/monthly.csv", "notebooks/recon.py", "data/nobody.csv"]
    )
    assert set(got) == {"data/sales.csv", "data/monthly.csv"}
    assert got["data/sales.csv"]["readers"] == 2 and got["data/sales.csv"]["writers"] == 1
    assert got["data/monthly.csv"]["writers"] == 1
    assert "readers" not in got["data/monthly.csv"]  # omitted, NOT reported as 0
    for entry in got.values():
        # No count is ever zero: an absent key is silence, a zero would be a finding.
        assert all(entry[k] > 0 for k in ("readers", "writers") if k in entry)
    assert "data/nobody.csv" not in got  # nothing recorded -> no payload at all


def test_counts_date_every_claim_by_its_weakest_link(tmp_path):
    # A count is only as current as its stalest member: one notebook that ran this morning
    # must not vouch for another that has not run since March, so `as_of` is the OLDEST
    # contributing receipt and `stale` is computed from that.
    graph = lineage.from_receipts(
        [
            _receipt("fresh.py", updated="2026-07-30T09:00:00+00:00", reads=["d.csv"]),
            _receipt("old.py", updated="2026-01-05T09:00:00+00:00", reads=["d.csv"]),
        ]
    )
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    entry = lineage.counts(graph, ["d.csv"], now)["d.csv"]
    assert entry["readers"] == 2
    assert entry["as_of"] == "2026-01-05T09:00:00+00:00"  # the oldest, not the newest
    assert entry["stale"] is True


def test_a_recent_claim_is_not_marked_stale(tmp_path):
    graph = lineage.from_receipts(
        [_receipt("a.py", updated="2026-07-30T09:00:00+00:00", reads=["d.csv"])]
    )
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    assert lineage.counts(graph, ["d.csv"], now)["d.csv"]["stale"] is False


def test_an_undateable_claim_counts_as_stale():
    # Fails toward showing the caveat: a claim whose age cannot be established is exactly
    # the one a user should be told to re-check.
    assert lineage.is_stale("") is True
    assert lineage.is_stale("not-a-timestamp") is True


def test_paths_are_matched_after_normalisation(tmp_path):
    graph = lineage.build(_chain(tmp_path))
    for spelling in ("data/sales.csv", "./data/sales.csv", "data\\sales.csv", "x/../data/sales.csv"):
        assert len(lineage.readers(graph, spelling)) == 2, spelling


def test_a_deleted_notebooks_edges_disappear(tmp_path):
    # Matches inputs.read_results: a receipt whose notebook is gone is dropped, so the
    # graph never claims a deleted notebook still reads a file.
    ws = _chain(tmp_path)
    assert len(lineage.readers(lineage.build(ws), "data/sales.csv")) == 2
    (ws / "notebooks" / "board.py").unlink()
    graph = lineage.build(ws)
    assert lineage.readers(graph, "data/sales.csv") == ("notebooks/recon.py",)
    assert "notebooks/board.py" not in graph.notebooks


def test_a_name_only_fingerprint_joins_nothing(tmp_path):
    # No path means no content guarantee AND no lineage edge — the path is the join.
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    g = {"mi": mi, "df": pl.DataFrame({"a": [1]}), "__file__": str(ws / "notebooks" / "recon.py")}
    exec("mi.fingerprint(df, 'in_memory')", g)
    graph = lineage.build(ws)
    assert graph.readers == {} and graph.display == {}
    assert graph.notebooks == ("notebooks/recon.py",)  # it still counts for coverage


def test_corrupt_and_foreign_receipts_are_skipped(tmp_path):
    ws = _ws(tmp_path)
    directory = inputs.inputs_dir(ws)
    directory.mkdir(parents=True)
    (directory / "corrupt.json").write_text("{not json", "utf-8")
    (directory / "foreign.json").write_text(json.dumps(["a", "list"]), "utf-8")
    graph = lineage.build(ws)
    assert graph == lineage.Graph()
    assert lineage.readers(graph, "data/x.csv") == ()


def test_an_old_receipt_without_rel_contributes_no_edge(tmp_path):
    # A receipt predating `rel` holds only the path AS WRITTEN — unresolved, and therefore
    # not known to name the workspace-relative file of the same spelling. Joining it anyway
    # would FABRICATE an edge, which _key's docstring calls the one failure worse than a
    # missing one, so it is dropped. Bounded: the notebook's next run re-records it.
    ws = _ws(tmp_path, "recon.py", "board.py")
    directory = inputs.inputs_dir(ws)
    directory.mkdir(parents=True)
    (directory / "notebooks__recon.py.json").write_text(
        json.dumps(
            {
                "notebook": "notebooks/recon.py",
                "updated": "2026-01-01T00:00:00+00:00",
                "inputs": {"sales": {"path": "data/sales.csv", "sha": "a" * 64, "changed": False}},
            }
        ),
        "utf-8",
    )
    mi = _load_payload(ws)
    _data(ws, "data/sales.csv")
    _writes(mi, ws, "notebooks/board.py", "data/sales.csv")
    graph = lineage.build(ws)
    assert lineage.readers(graph, "data/sales.csv") == ()  # the guess is NOT made
    assert lineage.writers(graph, "data/sales.csv") == ("notebooks/board.py",)
    assert "notebooks/recon.py" in graph.notebooks  # but it still counts for coverage


def test_an_unidentified_notebook_is_not_a_node(tmp_path):
    # Every receipt whose notebook could not be detected shares ONE "_notebook" bucket and
    # overwrites the last, so it is an unknown — not a reader to name and not a countable
    # unit of coverage.
    graph = lineage.from_receipts(
        [
            _receipt(inputs.UNKNOWN_NOTEBOOK, reads=["data/sales.csv"]),
            _receipt("notebooks/recon.py", reads=["data/sales.csv"]),
        ]
    )
    assert lineage.readers(graph, "data/sales.csv") == ("notebooks/recon.py",)
    assert graph.notebooks == ("notebooks/recon.py",)
    assert "1 notebook(s)" in lineage.coverage_note(graph)  # the denominator isn't inflated


def test_case_folding_follows_the_platform():
    # Two spellings differing only in case are ONE file on Windows (fold, or silently lose
    # the edge) and TWO on POSIX (do not fold, or fabricate one). Driven through
    # from_receipts because Path.resolve() canonicalises case for files that exist, which
    # would mask the difference in a runtime-driven test.
    graph = lineage.from_receipts(
        [
            _receipt("a.py", reads=["Data/Sales.csv"]),
            _receipt("b.py", reads=["data/sales.csv"]),
        ]
    )
    if os.name == "nt":
        assert lineage.readers(graph, "data/sales.csv") == ("a.py", "b.py")  # one node
        assert lineage.readers(graph, "DATA/SALES.CSV") == ("a.py", "b.py")
    else:
        assert lineage.readers(graph, "data/sales.csv") == ("b.py",)  # two distinct files
        assert lineage.readers(graph, "Data/Sales.csv") == ("a.py",)


def test_readers_are_sorted_not_in_receipt_order():
    # Receipt order is an accident of the slug filenames; a list a user is shown must not
    # reshuffle between runs, so the graph sorts.
    graph = lineage.from_receipts(
        [_receipt("z.py", reads=["d.csv"]), _receipt("a.py", reads=["d.csv"])]
    )
    assert lineage.readers(graph, "d.csv") == ("a.py", "z.py")


def test_a_notebook_reached_by_several_datasets_appears_once():
    # The diamond: two of x.csv's readers both write into y.csv, which c.py reads. c.py is
    # reachable twice and must be expanded — and reported — exactly once.
    graph = lineage.from_receipts(
        [
            _receipt("a.py", reads=["x.csv"], writes=["y.csv"]),
            _receipt("b.py", reads=["x.csv"], writes=["y.csv"]),
            _receipt("c.py", reads=["y.csv"], writes=["z.csv"]),
        ]
    )
    impact = lineage.downstream(graph, "x.csv")
    assert impact.notebooks == ("a.py", "b.py", "c.py")
    assert impact.datasets == ("y.csv", "z.csv")


def test_the_graph_never_carries_a_data_value(tmp_path):
    # The receipts are value-free, so the graph derived from them is too. Belt and braces:
    # the sentinel is in the DATA, in a column NAME, and in the file NAME.
    ws = _ws(tmp_path)
    mi = _load_payload(ws)
    _data(ws, "data/sales.csv", f"id,{SECRET}\n1,{SECRET}\n")
    _reads(mi, ws, "notebooks/recon.py", "data/sales.csv")
    graph = lineage.build(ws)
    blob = repr(graph) + lineage.coverage_note(graph)
    blob += repr(lineage.downstream(graph, "data/sales.csv"))
    blob += repr(lineage.counts(graph, ["data/sales.csv"]))
    assert SECRET not in blob


def test_coverage_note_never_implies_completeness(tmp_path):
    empty = lineage.coverage_note(lineage.Graph())
    assert "not the same as there being none" in empty  # the empty case is the dangerous one
    populated = lineage.coverage_note(lineage.build(_chain(tmp_path)))
    assert "3 notebook(s)" in populated  # says how much it saw, so the floor is quantified
    assert "floor" in populated and "NOT evidence" in populated


# -- hub wiring: the impact warning where the user is about to act ---------------


def _hub(tmp_path, monkeypatch):
    from mooring import config, paths
    from mooring.hub.server import Hub

    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    monkeypatch.delenv("MOORING_TOKEN", raising=False)
    ws = tmp_path / "ws"
    (ws / "notebooks").mkdir(parents=True)
    spec = config.RepoSpec(alias="ws", owner="", repo="", workspace_path=str(ws))
    return Hub(config.AppConfig(repos=(spec,), active_alias="ws")), ws


def _report(*rels):
    from mooring import sync

    return sync.StatusReport(
        head_commit="",
        files=[
            sync.FileStatus(path=rel, state=sync.FileState.NEW_LOCAL, local_sha="x")
            for rel in rels
        ],
    )


def test_state_row_carries_the_reader_count(tmp_path, monkeypatch):
    hub, ws = _hub(tmp_path, monkeypatch)
    mi = _load_payload(ws)
    (ws / "notebooks" / "recon.py").write_text("# notebook\n", "utf-8")
    _data(ws, "data/sales.csv")
    _reads(mi, ws, "notebooks/recon.py", "data/sales.csv")

    files, _ = hub._files_artifacts(_report("data/sales.csv", "notebooks/recon.py"), ws)
    row = next(f for f in files if f["path"] == "data/sales.csv")
    assert row["lineage"]["readers"] == 1
    assert "writers" not in row["lineage"]  # nothing writes it, and nothing SAYS so
    assert row["lineage"]["stale"] is False and row["lineage"]["as_of"]
    # The notebook doing the reading is not itself read by anything, so it claims nothing.
    assert "lineage" not in next(f for f in files if f["path"] == "notebooks/recon.py")


def test_a_generated_output_row_never_claims_it_has_no_readers(tmp_path, monkeypatch):
    # The row the reviewer flagged, end to end through the real Hub: an extract WRITTEN by
    # a notebook and read by none must ship a writers claim and no readers key at all —
    # it is the file most likely to be read by something mooring cannot see.
    hub, ws = _hub(tmp_path, monkeypatch)
    mi = _load_payload(ws)
    (ws / "notebooks" / "recon.py").write_text("# notebook\n", "utf-8")
    _data(ws, "data/monthly.csv")
    _writes(mi, ws, "notebooks/recon.py", "data/monthly.csv")

    files, _ = hub._files_artifacts(_report("data/monthly.csv"), ws)
    row = next(f for f in files if f["path"] == "data/monthly.csv")
    assert row["lineage"]["writers"] == 1
    assert "readers" not in row["lineage"]  # the badge is never handed a zero to render


def test_state_row_omits_lineage_when_nothing_is_recorded(tmp_path, monkeypatch):
    # A row without the badge means "nothing recorded", which the UI must not be able to
    # render as "safe to overwrite" — so the key is ABSENT rather than zeroed.
    hub, ws = _hub(tmp_path, monkeypatch)
    _data(ws, "data/sales.csv")
    files, _ = hub._files_artifacts(_report("data/sales.csv"), ws)
    assert "lineage" not in files[0]
