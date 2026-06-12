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


def test_review_roundtrip(tmp_path):
    mft = manifest.Manifest(
        branch="main",
        review_branch="mooring/phil/20260612-0900",
        review_files={"notebooks/a.py": "sha1", "notebooks/gone.py": None},
    )
    manifest.save(tmp_path, mft)
    loaded = manifest.load(tmp_path)
    assert loaded.review_branch == "mooring/phil/20260612-0900"
    assert loaded.review_files == {"notebooks/a.py": "sha1", "notebooks/gone.py": None}


def test_load_old_manifest_defaults_review(tmp_path):
    manifest.save(tmp_path, manifest.Manifest(branch="main", head_commit="abc"))
    loaded = manifest.load(tmp_path)
    assert loaded.review_branch == ""
    assert loaded.review_files == {}


def test_save_without_review_omits_key(tmp_path):
    manifest.save(tmp_path, manifest.Manifest(branch="main"))
    raw = manifest.manifest_path(tmp_path).read_text("utf-8")
    assert "review" not in raw
