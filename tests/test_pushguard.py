"""The push guard orchestrator: detectors, allowlist, pragma, tokens, heuristic."""

from mooring import pushguard

SECRET_VALUE_DO_NOT_LEAK = "ghp_" + "a" * 40  # a well-formed GitHub token shape


def test_secret_and_pii_findings_merge():
    src = (
        "import polars as pl\n"
        f'TOKEN = "{SECRET_VALUE_DO_NOT_LEAK}"\n'
        "contact = 'jane.doe@example.com'\n"
    )
    findings = pushguard.scan_text("notebooks/a.py", src.encode())
    kinds = {f.kind for f in findings}
    assert "GitHub token" in kinds
    assert "email address" in kinds
    assert {f.line for f in findings} == {2, 3}


def test_findings_are_value_free():
    src = f'TOKEN = "{SECRET_VALUE_DO_NOT_LEAK}"\n'
    findings = pushguard.scan_text("notebooks/a.py", src.encode())
    assert findings
    for f in findings:
        assert SECRET_VALUE_DO_NOT_LEAK not in f.kind
        assert SECRET_VALUE_DO_NOT_LEAK not in str(f.line)
    for desc in pushguard.describe(findings):
        assert SECRET_VALUE_DO_NOT_LEAK not in desc
    token = pushguard.file_token("notebooks/a.py", src.encode(), findings)
    assert SECRET_VALUE_DO_NOT_LEAK not in token


def test_scan_never_modifies_bytes():
    data = bytearray(f'TOKEN = "{SECRET_VALUE_DO_NOT_LEAK}"\n'.encode())
    before = bytes(data)
    pushguard.scan_text("notebooks/a.py", bytes(data))
    assert bytes(data) == before


def test_push_ok_pragma_retires_a_line():
    src = f'TOKEN = "{SECRET_VALUE_DO_NOT_LEAK}"  # mooring: push-ok\n'
    assert pushguard.scan_text("notebooks/a.py", src.encode()) == []
    # The pragma is line-scoped: another line still fires.
    src2 = src + f'OTHER = "{SECRET_VALUE_DO_NOT_LEAK}"\n'
    findings = pushguard.scan_text("notebooks/a.py", src2.encode())
    assert [f.line for f in findings] == [2]


def test_non_text_extensions_pass_through():
    data = f"{SECRET_VALUE_DO_NOT_LEAK}".encode()
    assert pushguard.scan_text("data/blob.parquet", data) == []
    assert pushguard.scan_text("assets/logo.png", data) == []


def test_raw_data_heuristic_fires_only_on_big_consistent_tables():
    big = "\n".join("a,b,c" for _ in range(1500)).encode()
    findings = pushguard.scan_text("data/export.csv", big)
    assert any("bulk data export" in f.kind for f in findings)
    # Small tables and inconsistent files never trip it.
    small = "\n".join("a,b,c" for _ in range(500)).encode()
    assert pushguard.scan_text("data/lookup.csv", small) == []
    ragged = ("a,b,c\n" + "\n".join("x" for _ in range(1500))).encode()
    assert pushguard.scan_text("data/notes.csv", ragged) == []
    # Two columns are below the conservative floor (a keyed lookup, not an export).
    two_col = "\n".join("k,v" for _ in range(1500)).encode()
    assert pushguard.scan_text("data/map.csv", two_col) == []


def test_file_token_binds_findings_and_bytes():
    src = f'TOKEN = "{SECRET_VALUE_DO_NOT_LEAK}"\n'.encode()
    findings = pushguard.scan_text("notebooks/a.py", src)
    t1 = pushguard.file_token("notebooks/a.py", src, findings)
    assert t1 == pushguard.file_token("notebooks/a.py", src, findings)  # stable
    # Different bytes -> different token (an old confirm can't cover an edit).
    src2 = src + b"# comment\n"
    assert pushguard.file_token("notebooks/a.py", src2, findings) != t1
    # Different path -> different token.
    assert pushguard.file_token("notebooks/b.py", src, findings) != t1


def test_make_guard_allowlist_and_collection():
    src = f'TOKEN = "{SECRET_VALUE_DO_NOT_LEAK}"\n'.encode()
    guard_fn, collected = pushguard.make_guard()
    descriptions = guard_fn("notebooks/a.py", src)
    assert descriptions and "GitHub token" in descriptions[0]
    assert "notebooks/a.py" in collected
    token = collected["notebooks/a.py"]["token"]
    # Acknowledged: the same file with the same bytes passes.
    allowed_fn, allowed_collected = pushguard.make_guard(frozenset({token}))
    assert allowed_fn("notebooks/a.py", src) == []
    assert allowed_collected == {}
    # But changed bytes invalidate the acknowledgement.
    assert allowed_fn("notebooks/a.py", src + b"# edit\n") != []


def test_clean_file_yields_nothing():
    guard_fn, collected = pushguard.make_guard()
    assert guard_fn("notebooks/a.py", b"import marimo\napp = marimo.App()\n") == []
    assert collected == {}
