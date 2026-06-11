from mooring import gitsha


def test_blob_sha_matches_git_hash_object():
    # known values from `git hash-object`
    assert gitsha.blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert gitsha.blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_python_files_are_normalized_to_lf(tmp_path):
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(b"import marimo\nprint(1)\n")
    crlf.write_bytes(b"import marimo\r\nprint(1)\r\n")
    assert gitsha.local_blob_sha(lf, "notebooks/lf.py") == gitsha.local_blob_sha(
        crlf, "notebooks/crlf.py"
    )
    assert gitsha.read_for_push(crlf, "notebooks/crlf.py") == b"import marimo\nprint(1)\n"


def test_data_files_are_byte_faithful(tmp_path):
    data = tmp_path / "table.csv"
    data.write_bytes(b"a,b\r\n1,2\r\n")
    assert gitsha.read_for_push(data, "data/table.csv") == b"a,b\r\n1,2\r\n"
    assert gitsha.local_blob_sha(data, "data/table.csv") == gitsha.blob_sha(b"a,b\r\n1,2\r\n")
