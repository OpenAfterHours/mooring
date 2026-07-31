"""`mooring lineage` — the CLI view of the recorded read/write graph.

All local: it reads the sync-excluded ``.mooring/inputs`` receipts off disk, so no GitHub
login is involved. The receipts are written by hand here because what is under test is the
RENDERING (and above all that the coverage caveat can never go missing); the graph itself
is exercised against the real injected runtime in test_lineage.py.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from mooring import cli, inputs, lineage, paths

# Relative to the real clock, never hardcoded: a fixed date would silently cross the
# staleness threshold as time passes and turn these into a slow time bomb.
FRESH = datetime.now(timezone.utc).isoformat(timespec="seconds")
LONG_AGO = (
    datetime.now(timezone.utc) - timedelta(days=lineage.STALE_AFTER_DAYS + 60)
).isoformat(timespec="seconds")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "appdata")
    ws = tmp_path / "ws"
    monkeypatch.setenv("MOORING_CLIENT_ID", "cid")
    monkeypatch.setenv("MOORING_OWNER", "acme")
    monkeypatch.setenv("MOORING_REPO", "nbs")
    monkeypatch.setenv("MOORING_WORKSPACE", str(ws))
    monkeypatch.setenv("MOORING_TRUSTSTORE", "0")
    for var in ("MOORING_BRANCH", "MOORING_ACTIVE_REPO", "MOORING_GITHUB_HOST"):
        monkeypatch.delenv(var, raising=False)
    (ws / "notebooks").mkdir(parents=True, exist_ok=True)
    return ws


def _entry(rel):
    return {"path": rel, "rel": rel, "hashed": True, "sha": "a" * 64, "changed": False}


def _receipt(ws, notebook, reads=(), writes=(), updated=FRESH):
    (ws / notebook).parent.mkdir(parents=True, exist_ok=True)
    (ws / notebook).write_text("# notebook\n", "utf-8")
    directory = inputs.inputs_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    slug = notebook.replace("_", "_u").replace("/", "__")
    (directory / f"{slug}.json").write_text(
        json.dumps(
            {
                "notebook": notebook,
                "updated": updated,
                "inputs": {rel: _entry(rel) for rel in reads},
                "outputs": {rel: _entry(rel) for rel in writes},
            }
        ),
        "utf-8",
    )


def _chain(ws):
    _receipt(ws, "notebooks/ingest.py", reads=["data/raw.csv"], writes=["data/sales.csv"])
    _receipt(ws, "notebooks/recon.py", reads=["data/sales.csv"], writes=["data/monthly.csv"])
    _receipt(ws, "notebooks/board.py", reads=["data/sales.csv"])


def test_lineage_lists_every_recorded_file(workspace, capsys):
    _chain(workspace)
    assert cli.main(["lineage"]) == 0
    out = capsys.readouterr().out
    assert "data/sales.csv" in out and "data/raw.csv" in out and "data/monthly.csv" in out
    assert "written by  notebooks/ingest.py" in out
    assert "notebooks/board.py" in out and "notebooks/recon.py" in out
    assert "3 notebook(s)" in out and "floor" in out  # the caveat rides along
    assert out.count(FRESH[:10]) >= 3  # every notebook named is dated


def test_a_claim_no_run_has_confirmed_says_so(workspace, capsys):
    # The failure mode this dating exists for: b.py fingerprinted the file months ago,
    # the analyst deleted that line, and the receipt goes on asserting the dependency
    # until b.py next runs mi.reset(). It must not be able to read as current.
    _receipt(workspace, "notebooks/stale.py", reads=["data/sales.csv"], updated=LONG_AGO)
    _receipt(workspace, "notebooks/live.py", reads=["data/sales.csv"])
    assert cli.main(["lineage", "data/sales.csv"]) == 0
    out = capsys.readouterr().out
    assert f"notebooks/stale.py ({LONG_AGO[:10]}, not confirmed since)" in out
    assert f"notebooks/live.py ({FRESH[:10]})" in out  # dated, but not flagged


def test_lineage_for_one_path_answers_what_breaks(workspace, capsys):
    _chain(workspace)
    assert cli.main(["lineage", "data/sales.csv"]) == 0
    out = capsys.readouterr().out
    assert "Recorded readers (2)" in out
    assert "notebooks/board.py" in out and "notebooks/recon.py" in out
    assert "Recorded writers (1)" in out and "notebooks/ingest.py" in out
    # The transitive tail: recon.py rewrites monthly.csv, so a change to sales.csv reaches it.
    assert "Further downstream" in out and "data/monthly.csv" in out
    assert "Built from:" in out and "data/raw.csv" in out


def test_lineage_accepts_a_windows_spelling(workspace, capsys):
    _chain(workspace)
    assert cli.main(["lineage", "data\\sales.csv"]) == 0
    assert "Recorded readers (2)" in capsys.readouterr().out


def test_an_unrecorded_path_is_never_reported_as_safe(workspace, capsys):
    # THE honesty case. "Nothing reads this" is the answer a user could act on
    # dangerously, so it must never appear without the sentence saying what the graph
    # cannot see — and it must not use reassuring words like "safe" or "unused".
    _chain(workspace)
    assert cli.main(["lineage", "data/nobody-knows.csv"]) == 0
    out = capsys.readouterr().out
    assert "Nothing recorded reads or writes this." in out
    assert "floor" in out and "NOT evidence" in out
    assert "safe" not in out.lower() and "unused" not in out.lower()


def test_empty_workspace_says_what_it_does_not_know(workspace, capsys):
    assert cli.main(["lineage"]) == 0
    out = capsys.readouterr().out
    assert "No lineage recorded yet" in out
    assert "not the same as there being none" in out


def test_lineage_survives_a_corrupt_receipt(workspace, capsys):
    # Local state is best-effort: a mangled receipt is skipped, never a traceback.
    inputs.inputs_dir(workspace).mkdir(parents=True, exist_ok=True)
    (inputs.inputs_dir(workspace) / "broken.json").write_text("{not json", "utf-8")
    _receipt(workspace, "notebooks/recon.py", reads=["data/sales.csv"])
    assert cli.main(["lineage", "data/sales.csv"]) == 0
    assert "notebooks/recon.py" in capsys.readouterr().out


def test_inputs_command_reports_both_sides(workspace, capsys):
    _receipt(workspace, "notebooks/recon.py", reads=["data/sales.csv"], writes=["data/out.csv"])
    assert cli.main(["inputs"]) == 0
    out = capsys.readouterr().out
    assert "1 input(s) + 1 output(s) pinned, unchanged" in out
