import pytest

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


def test_last_push_roundtrips(tmp_path):
    from mooring import manifest

    m = manifest.Manifest(
        branch="main",
        files={"notebooks/a.py": "sha-new"},
        last_push={
            "notebooks/a.py": {"prev": "sha-old", "new": "sha-new"},
            "notebooks/new.py": {"prev": None, "new": "sha-n"},
        },
        last_push_branch="main",
    )
    manifest.save(tmp_path, m)
    loaded = manifest.load(tmp_path)
    assert loaded.last_push == m.last_push
    assert loaded.last_push_branch == "main"


def test_manifest_without_last_push_still_loads(tmp_path):
    from mooring import manifest

    m = manifest.Manifest(branch="main", files={"a.py": "s"})
    manifest.save(tmp_path, m)  # writes no last_push section (empty)
    loaded = manifest.load(tmp_path)
    assert loaded.last_push == {}
    assert loaded.last_push_branch == ""


# -- the remote-view cache (the offline fallback's input) ----------------------


def test_remote_cache_roundtrip_and_atomic_write(tmp_path):
    cache = manifest.RemoteCache(
        head_commit="abc",
        fetched_at="2026-07-02T09:00:00+00:00",
        files={"notebooks/a.py": "sha1", "data/x.csv": "sha2"},
        scope_folders=("notebooks", "data"),
        scope_exclude=("scratch",),
    )
    manifest.save_cache(tmp_path, cache)
    assert manifest.load_cache(tmp_path) == cache
    # atomic write: no stray temp file left behind (the manifest.save idiom)
    assert list((tmp_path / ".mooring").glob("*.tmp")) == []
    # the cache is a SIBLING of the manifest, never the manifest itself
    assert manifest.cache_path(tmp_path) != manifest.manifest_path(tmp_path)
    assert not manifest.manifest_path(tmp_path).exists()


def test_load_cache_missing_returns_none(tmp_path):
    assert manifest.load_cache(tmp_path) is None


@pytest.mark.parametrize(
    "payload",
    ["{not json", '"a bare string"', "[1, 2]", '{"files": [1, 2]}', '{"scope_folders": 3}'],
)
def test_load_cache_corrupt_returns_none(tmp_path, payload):
    # Fail-soft by contract: the cache is display-only, so a broken one means
    # "no offline view", never an exception.
    path = manifest.cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(payload, "utf-8")
    assert manifest.load_cache(tmp_path) is None
