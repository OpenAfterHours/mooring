"""The push guard orchestrator: detectors, allowlist, pragma, tokens, heuristic."""

import ast

import pytest

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


def test_oversized_text_file_is_flagged_not_skipped():
    big = b"just text\n" * (500_000)  # ~5 MB, over the scan cap
    findings = pushguard.scan_text("data/dump.txt", big)
    assert findings
    assert "too big to scan" in findings[0].kind


# -- the destructive-code detector (mooring.ai.codeguard at the push seam) -------
#
# The THRESHOLD is the point of these: only `floor` withholds a push. An `ask` finding
# is ordinary analyst work heading for the team repo, and a guard that fires on ordinary
# work gets clicked through — taking the secret and PII findings above it along.


def _cell(body: str) -> bytes:
    """A marimo-shaped notebook whose one cell is ``body`` (line 6 onwards)."""
    return (
        "import marimo\n"
        "app = marimo.App()\n\n\n"
        "@app.cell\n"
        f"def _():\n    {body}\n    return\n"
    ).encode()


def _raw(cell_body: str) -> bytes:
    """Like :func:`_cell` but ``cell_body`` carries its OWN indentation, so a test can
    lay out a multi-line construct exactly as an analyst would write it. The cell body
    starts on line 7."""
    return (
        "import marimo\napp = marimo.App()\n\n\n@app.cell\ndef _():\n" + cell_body + "    return\n"
    ).encode()


@pytest.mark.parametrize(
    ("body", "label"),
    [
        ("import os\n    os.remove('stale.csv')", "Deletes files or folders"),
        ("import shutil\n    shutil.rmtree('old')", "Deletes files or folders"),
        ("import subprocess\n    subprocess.run(['ls'])", "Runs another program on your computer"),
        ("con.execute('DROP TABLE sales')", "Deletes a database table, or every row in one"),
        ("exec(user_code)", "Runs code that is built while it runs"),
        ("open('mooring.toml', 'w').write(cfg)", "Changes mooring's own settings files"),
    ],
)
def test_floor_band_code_withholds_a_push_with_a_value_free_finding(body, label):
    data = _cell(body)
    findings = pushguard.scan_text("notebooks/a.py", data)
    assert label in {f.kind for f in findings}
    assert all(f.line >= 6 for f in findings)  # inside the cell, not the whole file
    guard_fn, collected = pushguard.make_guard()
    lines = guard_fn("notebooks/a.py", data)
    assert any(label in line for line in lines)
    assert "notebooks/a.py" in collected


@pytest.mark.parametrize(
    "body",
    [
        "df.to_csv('out.csv')",  # overwrites_file — THE regression that matters
        "df.to_parquet('snapshot.parquet')",
        "fig.savefig('chart.png')",
        "con.execute('INSERT INTO audit VALUES (1)')",  # changes_database
        "requests.post(url, json=payload)",  # sends_data
        "import polars as pl\n    df = pl.read_csv('in.csv')",  # a plain read: clean
    ],
)
def test_ask_band_code_never_withholds_a_push(body):
    """An ordinary analysis notebook must push without a prompt. Writing files, loading a
    warehouse table and calling an API are what these notebooks DO; `ask` is a statement
    about the author's own Undo at Apply time, not about what a teammate inherits."""
    data = _cell(body)
    assert pushguard.scan_text("notebooks/a.py", data) == []
    guard_fn, collected = pushguard.make_guard()
    assert guard_fn("notebooks/a.py", data) == []
    assert collected == {}


def test_unparseable_notebook_does_not_withhold_a_push():
    # `unparseable` is band ask, so a notebook the gate could not read is not a reason to
    # withhold someone's work; the content scanners still ran over it.
    broken = b"import marimo\napp = marimo.App()\n\n@app.cell\ndef _(:\n"
    assert pushguard.scan_text("notebooks/broken.py", broken) == []
    assert [f.kind for f in pushguard.code_findings("notebooks/broken.py", broken)] == [
        "unparseable"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "import shutil\n    shutil.rmtree('archive')",
        "import subprocess\n    subprocess.run(['rm', '-rf', 'x'])",
        "import os\n    os.remove('stale.csv')",
    ],
)
def test_a_plain_helper_module_is_never_classified(body):
    """THE scope decision, measured. codeguard was tuned against one marimo CELL; a
    module is a different shape, and against real module code the difference is not
    subtle — 0% of 70 notebook sources in this repo carry a floor finding, against 27.6%
    of `src/mooring`'s 127 modules. Those module hits are ~97% TRUE `deletes_files` /
    `runs_program`: 47% are `Path.unlink()` and 18% are `os.replace(tmp, path)`, the
    correct atomic-save idiom. Withholding a quarter of every helper module for writing
    files safely would make the dialog that ALSO carries the secret and PII findings
    into noise. So a module is out of scope, at the push seam and in the reviewer inbox
    alike."""
    module = f"import shutil\nimport os\n\n\ndef cleanup():\n    {body}\n".encode()
    assert pushguard.scan_text("codelib/helpers.py", module) == []
    assert pushguard.code_findings("codelib/helpers.py", module) == []
    # …while the SAME code inside a notebook is withheld. The scope is the shape of the
    # file, not a softening of what the classifier finds.
    assert pushguard.scan_text("notebooks/a.py", _cell(body)) != []


def test_the_atomic_save_idiom_is_the_reason_modules_are_out_of_scope():
    # `write a temp file, then os.replace it into place` is CORRECT Python and the single
    # biggest source of floor findings in real module code. Pinned so nobody "fixes" the
    # scope back to every .py without meeting this case first.
    module = (
        "import os\nfrom pathlib import Path\n\n\n"
        "def save(path, text):\n"
        "    tmp = Path(str(path) + '.tmp')\n"
        "    tmp.write_text(text, 'utf-8')\n"
        "    os.replace(tmp, path)\n"
    )
    assert pushguard.code_findings("codelib/io.py", module.encode()) == []
    # It is a genuine floor finding — the scope is what spares it, not a missing rule.
    assert any(
        f.kind == "deletes_files"
        for f in pushguard.code_findings("notebooks/a.py", _cell("os.replace(tmp, path)"))
    )


def test_code_findings_are_value_free():
    # The deleted PATH is the value here — and a secret-shaped one, so this pins that
    # NEITHER detector firing on the line carries what it matched.
    data = _cell(f"import os\n    os.remove('{SECRET_VALUE_DO_NOT_LEAK}')")
    kinds = {f.kind for f in pushguard.scan_text("notebooks/a.py", data)}
    assert "Deletes files or folders" in kinds and "GitHub token" in kinds
    for f in pushguard.scan_text("notebooks/a.py", data):
        assert SECRET_VALUE_DO_NOT_LEAK not in f.kind
    for desc in pushguard.describe(pushguard.scan_text("notebooks/a.py", data)):
        assert SECRET_VALUE_DO_NOT_LEAK not in desc
    for f in pushguard.code_findings("notebooks/a.py", data):
        assert SECRET_VALUE_DO_NOT_LEAK not in f.kind + f.label + f.band


def test_push_ok_pragma_retires_a_code_finding():
    body = "import os\n    os.remove('stale.csv')  # mooring: push-ok"
    assert pushguard.scan_text("notebooks/a.py", _cell(body)) == []
    assert pushguard.code_findings("notebooks/a.py", _cell(body)) == []
    # Line-scoped, exactly like the secret pragma: a second call still fires.
    body2 = body + "\n    os.remove('other.csv')"
    assert [f.line for f in pushguard.scan_text("notebooks/a.py", _cell(body2))] == [9]


def test_push_ok_pragma_reaches_a_multi_line_call():
    """The valve has to be USABLE, not merely present. A push finding is a standing
    property of a file — withheld on every push, forever — so if an analyst cannot find
    the line to mark, the tax is permanent and the whole dialog gets clicked through.
    A finding reports the line the call STARTS on, which is where a reader would put the
    comment; the closing paren and the line above are the intuitive wrong guesses."""
    call = (
        "    import subprocess\n"
        "    subprocess.run(\n"
        "        ['pack'],\n"
        "        check=True,\n"
        "    )\n"
    )
    found = pushguard.code_findings("notebooks/a.py", _raw(call))
    assert [(f.line, f.kind) for f in found] == [(8, "runs_program")]
    assert _raw(call).decode().splitlines()[7].strip() == "subprocess.run("  # the call line
    # Marked after the opening paren: retired, and the code still parses.
    marked = call.replace("subprocess.run(\n", "subprocess.run(  # mooring: push-ok\n")
    assert pushguard.code_findings("notebooks/a.py", _raw(marked)) == []
    ast.parse(_raw(marked).decode())
    # The two intuitive wrong guesses do NOT retire it — the reported line number is
    # what tells the analyst where to mark, so `mooring scan` printing it is load-bearing.
    closing = call.replace("    )\n", "    )  # mooring: push-ok\n")
    assert pushguard.code_findings("notebooks/a.py", _raw(closing))
    above = call.replace("    subprocess.run(\n", "    # mooring: push-ok\n    subprocess.run(\n")
    assert pushguard.code_findings("notebooks/a.py", _raw(above))


def test_push_ok_pragma_cannot_reach_a_triple_quoted_sql_statement():
    """THE known hole in the valve, pinned so it stays known.

    A multi-line SQL string reports the line the STRING opens on — and the only text on
    that line is inside the literal, so the marker would be pasted into the query (it
    still parses as Python, so nothing warns; the SQL is simply wrong). The remedy is a
    single-line string, or a one-hop binding marked on the binding line, which the
    classifier follows. Both are checked here so the documented advice stays true."""
    tri = '    con.execute("""\n        DROP TABLE sales\n    """)\n'
    found = pushguard.code_findings("notebooks/a.py", _raw(tri))
    assert [(f.line, f.kind) for f in found] == [(7, "destroys_rows")]
    assert _raw(tri).decode().splitlines()[6].strip() == 'con.execute("""'  # inside the literal
    # Remedy 1: one line.
    one = "    con.execute('DROP TABLE sales')  # mooring: push-ok\n"
    assert pushguard.code_findings("notebooks/a.py", _raw(one)) == []
    # Remedy 2: bind one hop above and mark the BINDING — the classifier resolves it
    # there, so that is also where the finding (and so the pragma) belongs.
    bound = "    q = 'DROP TABLE sales'\n    con.execute(q)\n"
    assert [f.line for f in pushguard.code_findings("notebooks/a.py", _raw(bound))] == [7]
    marked = bound.replace("sales'\n", "sales'  # mooring: push-ok\n")
    assert pushguard.code_findings("notebooks/a.py", _raw(marked)) == []


def test_code_finding_binds_the_confirm_token():
    dirty = _cell("import os\n    os.remove('stale.csv')")
    guard_fn, collected = pushguard.make_guard()
    assert guard_fn("notebooks/a.py", dirty) != []
    token = collected["notebooks/a.py"]["token"]
    # Acknowledged ("Push anyway"): the same file with the same bytes passes.
    allowed_fn, allowed_collected = pushguard.make_guard(frozenset({token}))
    assert allowed_fn("notebooks/a.py", dirty) == []
    assert allowed_collected == {}
    # A second deletion is a NEW finding, so the old acknowledgement can't cover it.
    grown = _cell("import os\n    os.remove('stale.csv')\n    os.remove('older.csv')")
    assert allowed_fn("notebooks/a.py", grown) != []
    # …and any edit to the bytes invalidates it too, findings unchanged.
    assert allowed_fn("notebooks/a.py", dirty + b"# note\n") != []


def test_code_findings_keeps_bands_for_the_reviewer():
    """`code_findings` is the reviewer's view: every non-clean band, not just floor."""
    data = _cell("import os\n    df.to_csv('out.csv')\n    os.remove('stale.csv')")
    bands = {f.kind: f.band for f in pushguard.code_findings("notebooks/a.py", data)}
    assert bands == {"overwrites_file": "ask", "deletes_files": "floor"}
    # …while the push seam narrows that to the floor half.
    assert [f.kind for f in pushguard.scan_text("notebooks/a.py", data)] == [
        "Deletes files or folders"
    ]


def test_code_findings_skips_deletions_non_python_and_oversized():
    data = _cell("import os\n    os.remove('stale.csv')")
    assert pushguard.code_findings("notebooks/a.py", None) == []  # a deletion
    assert pushguard.code_findings("notes/howto.md", data) == []  # not Python
    assert pushguard.code_findings("data/dump.sql", data) == []
    over_cap = data + b"# pad\n" * 800_000  # ~5 MB, past the scan cap
    assert pushguard.code_findings("notebooks/a.py", over_cap) == []
    # …and the over-cap file is still flagged by the size rule, not waved through.
    assert any(
        "too big to scan" in f.kind for f in pushguard.scan_text("notebooks/a.py", over_cap)
    )


def test_a_real_analysis_notebook_pushes_without_a_prompt():
    """THE acceptance test for the threshold: a whole ordinary notebook — reads, a
    read-only `mo.sql`, a chart, a Deliver write, and prose that quotes a pip command —
    must be clean at BOTH bands. One false prompt per push and the guard is off."""
    nb = (
        'import marimo\n\n__generated_with = "0.23.9"\napp = marimo.App(width="medium")\n\n\n'
        "@app.cell\ndef _():\n    import marimo as mo\n    import polars as pl\n"
        "    import matplotlib.pyplot as plt\n    return mo, pl, plt\n\n\n"
        "@app.cell\ndef _(mo):\n"
        '    mo.md("""# Q3 recon\n\nRun `pip install polars` first if it is missing.""")\n'
        "    return\n\n\n"
        "@app.cell\ndef _(pl):\n"
        '    sales = pl.read_csv("data/sales.csv")\n'
        '    lookup = pl.read_parquet("data/lookup.parquet")\n    return lookup, sales\n\n\n'
        "@app.cell\ndef _(mo, sales):\n"
        '    result = mo.sql("SELECT region, sum(net) FROM sales GROUP BY region")\n'
        "    return (result,)\n\n\n"
        "@app.cell\ndef _(plt, result):\n    fig, ax = plt.subplots()\n"
        '    ax.bar(result["region"], result["sum"])\n'
        '    fig.savefig(".mooring/outbox/q3.png")\n    return\n\n\n'
        "@app.cell\ndef _(result):\n"
        '    result.write_csv(".mooring/outbox/q3.csv")\n    return\n\n\n'
        'if __name__ == "__main__":\n    app.run()\n'
    ).encode()
    assert pushguard.scan_text("notebooks/q3.py", nb) == []
    assert pushguard.code_findings("notebooks/q3.py", nb) == []


def test_a_markdown_file_is_not_run_through_the_code_classifier():
    # Prose that quotes destructive code is not code, and .md is not a candidate.
    doc = b"To clear the cache run:\n\n    import shutil\n    shutil.rmtree('cache')\n"
    assert pushguard.scan_text("docs/howto.md", doc) == []
