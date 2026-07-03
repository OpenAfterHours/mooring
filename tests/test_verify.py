"""The value-free run-verification receipts + their load-bearing SHA auto-clear.

The trust badge is only trustworthy if it *disappears* the instant the notebook it
vouches for changes. These pin: receipts are value-free, live under sync-excluded
.mooring, read back only while the content SHA still matches, and clear on demand.
"""

from __future__ import annotations

import json

from mooring import gitsha, sync, verify

NB = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"


def _write(ws, rel, text=NB):
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record(ws, rel, *, passed=True, cells_failed=None, text=NB):
    path = _write(ws, rel, text)
    sha = gitsha.local_blob_sha(path, rel)
    verify.record(
        ws, rel, passed=passed, sha=sha, cells_failed=cells_failed, ran_at="2026-07-03T09:00:00+00:00"
    )
    return sha


def test_verify_dir_is_structurally_unsyncable(tmp_path):
    # A receipt (or the throwaway HTML render) can NEVER be a sync candidate — it lives
    # under .mooring, which sync excludes structurally even against a custom exclude.
    for rel in (".mooring/verify/sales.py.json", ".mooring/verify/notebooks__nb.py.html"):
        assert sync.is_synced_path(rel) is False
        assert sync.is_synced_path(rel, exclude=("*.json",)) is False


def test_receipt_is_value_free(tmp_path):
    _record(tmp_path, "sales.py", passed=False, cells_failed=2)
    receipt = json.loads((tmp_path / ".mooring" / "verify" / "sales.py.json").read_text("utf-8"))
    # A boolean, a content hash, a count, and a timestamp — nothing else.
    assert set(receipt) == {"notebook", "sha", "passed", "cells_failed", "ran_at"}
    assert receipt["passed"] is False
    assert receipt["cells_failed"] == 2


def test_read_results_surfaces_a_matching_receipt(tmp_path):
    _record(tmp_path, "sales.py", passed=True)
    results = verify.read_results(tmp_path)
    assert results["sales.py"]["passed"] is True
    assert results["sales.py"]["ran_at"] == "2026-07-03T09:00:00+00:00"


def test_badge_auto_clears_when_the_notebook_changes(tmp_path):
    # THE load-bearing rule: verify vouches for exact bytes. Edit them and the receipt,
    # though still on disk, no longer matches the file SHA, so it is not surfaced.
    _record(tmp_path, "sales.py", passed=True)
    assert "sales.py" in verify.read_results(tmp_path)
    (tmp_path / "sales.py").write_text(NB + "\n# edited\n", encoding="utf-8")
    assert "sales.py" not in verify.read_results(tmp_path)


def test_read_results_drops_a_deleted_notebooks_receipt(tmp_path):
    _record(tmp_path, "sales.py", passed=True)
    (tmp_path / "sales.py").unlink()
    assert verify.read_results(tmp_path) == {}


def test_read_results_ignores_corrupt_and_foreign_receipts(tmp_path):
    _record(tmp_path, "good.py", passed=True)
    vdir = tmp_path / ".mooring" / "verify"
    (vdir / "corrupt.json").write_text("{not json", encoding="utf-8")
    (vdir / "foreign.json").write_text(json.dumps(["a", "list"]), encoding="utf-8")
    results = verify.read_results(tmp_path)
    assert set(results) == {"good.py"}


def test_slug_is_injective(tmp_path):
    # Two distinct paths that a naive slug would collide (a_b vs a/b) must not share a
    # receipt file, or one notebook's badge would overwrite the other's.
    assert verify._slug("a_b.py") != verify._slug("a/b.py")


def test_render_target_lives_in_the_verify_dir(tmp_path):
    target = verify.render_target(tmp_path, "notebooks/sales.py")
    assert target.parent == verify.verify_dir(tmp_path)
    assert target.suffix == ".html"
    assert sync.is_synced_path(target.relative_to(tmp_path).as_posix()) is False


def test_clear_all_and_one(tmp_path):
    _record(tmp_path, "a.py", passed=True)
    _record(tmp_path, "b.py", passed=True)
    assert verify.clear(tmp_path, "a.py") == 1
    assert set(verify.read_results(tmp_path)) == {"b.py"}
    assert verify.clear(tmp_path) == 1
    assert verify.read_results(tmp_path) == {}
