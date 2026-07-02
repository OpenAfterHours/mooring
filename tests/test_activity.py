"""The local activity ledger: append/read/filter/rotate, and the L0 purity pin."""

from mooring import activity


def test_record_and_read_newest_first(tmp_path):
    activity.record(tmp_path, "pull", summary="2 pulled")
    activity.record(tmp_path, "push", summary="1 pushed", path="notebooks/a.py")
    entries = activity.read(tmp_path)
    assert [e["op"] for e in entries] == ["push", "pull"]
    assert entries[0]["path"] == "notebooks/a.py"
    assert all("ts" in e for e in entries)


def test_read_filters_by_path_including_paths_lists(tmp_path):
    activity.record(tmp_path, "delete", path="notebooks/a.py", paths=["notebooks/a.py"])
    activity.record(tmp_path, "delete", path="reports/Sales.pbip", paths=["reports/Sales.pbip", "reports/Sales.SemanticModel/model.tmdl"])
    activity.record(tmp_path, "pull", summary="1 pulled")
    assert [e["op"] for e in activity.read(tmp_path, path="notebooks/a.py")] == ["delete"]
    hits = activity.read(tmp_path, path="reports/Sales.SemanticModel/model.tmdl")
    assert len(hits) == 1 and hits[0]["path"] == "reports/Sales.pbip"


def test_read_limit_and_corrupt_lines_skipped(tmp_path):
    for i in range(5):
        activity.record(tmp_path, "pull", summary=f"round {i}")
    ledger = tmp_path / ".mooring" / "activity.jsonl"
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write("{corrupt\n")
    entries = activity.read(tmp_path, limit=3)
    assert len(entries) == 3
    assert entries[0]["summary"] == "round 4"


def test_read_missing_ledger_is_empty(tmp_path):
    assert activity.read(tmp_path) == []


def test_rotation_bounds_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "_ROTATE_BYTES", 500)
    monkeypatch.setattr(activity, "_ROTATE_KEEP", 3)
    for i in range(30):
        activity.record(tmp_path, "pull", summary=f"round {i}")
    lines = (tmp_path / ".mooring" / "activity.jsonl").read_text("utf-8").splitlines()
    assert len(lines) <= 10  # rotation kicked in; far fewer than 30
    # The newest entry is always the last line.
    assert "round 29" in lines[-1]


def test_leaf_purity_trash_and_activity_import_only_l0():
    """Invariant pin (belt for the .importlinter braces): the safety-net leaves
    import only the L0 foundation (paths/gitsha), so they stay callable from
    anywhere — sync, deletion, the adapters — without dragging layers upward."""
    import ast
    import inspect

    import mooring.activity
    import mooring.trash

    allowed = {"mooring.paths", "mooring.gitsha"}
    for module in (mooring.trash, mooring.activity):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                if mod.startswith("mooring"):
                    assert mod in allowed, f"{module.__name__} imports {mod}"
