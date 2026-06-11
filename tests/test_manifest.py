from mooring import manifest


def test_load_missing_returns_empty(tmp_path):
    mft = manifest.load(tmp_path)
    assert mft.files == {}
    assert mft.head_commit == ""


def test_roundtrip(tmp_path):
    mft = manifest.Manifest(branch="main", head_commit="abc", files={"notebooks/a.py": "sha1"})
    manifest.save(tmp_path, mft)
    loaded = manifest.load(tmp_path)
    assert loaded.branch == "main"
    assert loaded.head_commit == "abc"
    assert loaded.files == {"notebooks/a.py": "sha1"}
    # no stray temp file left behind
    assert list((tmp_path / ".mooring").glob("*.tmp")) == []
