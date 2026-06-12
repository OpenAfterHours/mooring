"""PBIP artifact grouping, state aggregation, and launching."""

import os
from pathlib import Path

import pytest

from mooring import pbip
from mooring.sync import FileState, FileStatus


def fs(path, state=FileState.SYNCED):
    return FileStatus(path=path, state=state)


def test_group_single_artifact_with_strays():
    files = [
        fs("notebooks/a.py"),
        fs("reports/Sales.pbip"),
        fs("reports/Sales.SemanticModel/.platform"),
        fs("reports/Sales.SemanticModel/definition/model.tmdl"),
        fs("reports/Sales.Report/definition.pbir"),
        fs("reports/readme.md"),
    ]
    artifacts, member_paths = pbip.group(files)
    assert [a.key for a in artifacts] == ["reports/Sales"]
    assert artifacts[0].name == "Sales"
    assert artifacts[0].pointer == "reports/Sales.pbip"
    assert len(artifacts[0].members) == 4
    assert "notebooks/a.py" not in member_paths
    assert "reports/readme.md" not in member_paths


def test_group_two_artifacts_no_cross_contamination():
    files = [
        fs("reports/Sales.pbip"),
        fs("reports/Sales.Report/x.json"),
        fs("reports/SalesArchive.pbip"),
        fs("reports/SalesArchive.Report/x.json"),
    ]
    artifacts, _ = pbip.group(files)
    by_key = {a.key: a for a in artifacts}
    assert set(by_key) == {"reports/Sales", "reports/SalesArchive"}
    # prefix matching is on "<key>.Report/", so Sales must not swallow SalesArchive
    assert [m.path for m in by_key["reports/Sales"].members] == [
        "reports/Sales.pbip",
        "reports/Sales.Report/x.json",
    ]


def test_folder_without_pointer_stays_ungrouped():
    files = [fs("reports/Sales.SemanticModel/model.tmdl")]
    artifacts, member_paths = pbip.group(files)
    assert artifacts == []
    assert member_paths == set()


def test_remote_only_pointer_still_groups():
    files = [
        fs("reports/Sales.pbip", FileState.NEW_REMOTE),
        fs("reports/Sales.Report/x.json", FileState.NEW_REMOTE),
    ]
    artifacts, _ = pbip.group(files)
    assert len(artifacts) == 1
    assert len(artifacts[0].members) == 2


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([FileState.SYNCED, FileState.SYNCED], "synced"),
        ([FileState.SYNCED, FileState.MODIFIED], "modified"),
        ([FileState.NEW_LOCAL, FileState.DELETED_LOCAL], "modified"),
        ([FileState.SYNCED, FileState.NEW_REMOTE], "remote changed"),
        ([FileState.MODIFIED, FileState.REMOTE_CHANGED], "mixed"),
        ([FileState.MODIFIED, FileState.REMOTE_CHANGED, FileState.CONFLICT], "conflict"),
        ([FileState.SYNCED, FileState.CONFLICT], "conflict"),
        ([FileState.SYNCED, FileState.IN_REVIEW], "in review"),
        ([FileState.MODIFIED, FileState.IN_REVIEW], "modified"),
    ],
)
def test_aggregate_state(states, expected):
    members = [fs(f"reports/S.Report/{i}", s) for i, s in enumerate(states)]
    assert pbip.aggregate_state(members) == expected


def test_launch_without_startfile_raises(monkeypatch):
    monkeypatch.delattr(os, "startfile", raising=False)
    with pytest.raises(pbip.PbipLaunchError):
        pbip.launch(Path("x.pbip"))


def test_launch_uses_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(pbip.os, "startfile", opened.append, raising=False)
    pbip.launch(Path("reports/Sales.pbip"))
    assert opened == [Path("reports/Sales.pbip")]
