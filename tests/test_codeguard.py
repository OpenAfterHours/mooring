"""The Apply gate's static classifier: bands, the clean corpus, tokens, value-freedom.

The clean corpus is the most important thing in this file. The gate only works if it is
silent on ordinary analysis — an analyst who sees a prompt on ``df.to_csv`` turns the
feature off — so every false positive found here is worth more than a new detector.
"""

import pytest

from mooring.ai import codeguard

# A well-formed secret shape, planted inside code that DOES trip findings: the gate must
# classify it without ever carrying a slice of it into a finding, a description, or a
# token (the same fixture discipline as tests/test_pushguard.py).
SECRET_VALUE_DO_NOT_LEAK = "ghp_" + "a" * 40


# ---------------------------------------------------------------------------
# The clean corpus — none of this may fire
# ---------------------------------------------------------------------------

CLEAN_CELLS = [
    # the everyday shape of an analyst's cell
    'df = pd.read_csv("sales.csv")',
    'out = df[df.region == "North"].groupby("month").sum()',
    'fig = px.bar(out, x="month", y="revenue")',
    'result = mo.sql("SELECT region, sum(x) FROM df GROUP BY region")',
    "with open(path) as fh:\n    text = fh.read()",
    "def helper(x):\n    return x * 2",
    "import os\nimport shutil\nfrom pathlib import Path",
    'total = df["amount"].sum()\nax.set_title("Revenue")',
    'df2 = pl.read_parquet("input.parquet")',
    'p = Path("data") / "sales.csv"\ntext = p.read_text()',
    "plt.show()",
    'df.plot(x="a", y="b")',
    # reading is not writing, in every spelling
    'raw = open("notes.txt", "r").read()',
    'blob = open("data.bin", "rb").read()',
    'cfg = open("mooring.toml").read()',
    'rows = con.execute("SELECT * FROM sales WHERE region = \'North\'").fetchall()',
    "requests.get(url, timeout=5)",
    'urlopen("https://example.com/data.csv")',
    "data = json.loads(raw)",
    # a to_*/write_* with no destination returns a string; it writes nothing
    "csv_text = df.to_csv(index=False)",
    "summary = df.write_csv()",
    'payload = df.to_json(orient="records")',
    "buf = io.StringIO()\ndf.write_csv(buf)",
    "raw = io.BytesIO()\ndf.to_parquet(raw)",
    # creating, not destroying
    "os.makedirs(out_dir, exist_ok=True)",
    'os.listdir(".")',
    'joined = os.path.join(base, "x.csv")',
    'df.to_csv(".mooring/outbox/report.csv")',
    'fig.write_html(".mooring/outbox/report.html")',
    'Path(".mooring/outbox/summary.md").write_text(body)',
    # read-only SQL, in each of its leading keywords
    'con.execute("WITH recent AS (SELECT * FROM sales) SELECT count(*) FROM recent")',
    'con.execute("CREATE TABLE staging AS SELECT * FROM sales")',
    'con.execute("EXPLAIN SELECT * FROM sales")',
    'con.execute("PRAGMA table_info(sales)")',
    'con.execute("SHOW TABLES")',
    'con.execute("DESCRIBE sales")',
    # attribute names that collide with a detector but belong to someone else
    "arr = np.delete(arr, 0)",
    "q.put(item)",
    'sub = df.query("revenue > 100")',
    'val = df.eval("a + b")',
    "report = compile_report(df)",
    # prose that mentions dangerous things
    'note = "we used to DROP TABLE here"',
    '"""Helper that used to DROP TABLE staging every night."""\nx = 1',
    'mo.md("""# Sales\n\nRun `pip install polars` first.""")',
    'print("DELETE FROM staging is what we used to do")',
    'logger.info("pip install polars")',
    'status = "UPDATE complete"',
    'msg = "Insert your name here"',
    'labels = ["insert", "update", "delete"]',
    # English that opens with a SQL verb and even mentions a SQL noun
    'help_text = "DROP is how you remove a table"',
    'todo = "Update the set of columns by hand"',
    'todo = "Insert into the report a summary of the month"',
    'todo = "Merge into the summary workbook before sending"',
    # text a widget shows a human is never a statement
    'button = mo.ui.button(label="Delete from list")',
    'tab = mo.ui.tabs({"a": x}, label="Drop table view")',
    # a shadowed builtin is not the builtin
    'from re import compile\npattern = compile(r"\\d+")',
]


@pytest.mark.parametrize("code", CLEAN_CELLS)
def test_ordinary_analysis_never_fires(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_CLEAN, codeguard.describe(verdict)
    assert verdict.findings == ()


def test_a_commented_out_call_never_fires():
    code = "# os.remove(old)\n# shutil.rmtree(tmp)\nkeep = 1\n"
    assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN


def test_a_string_that_merely_names_a_verb_never_fires():
    # The whole reason this module uses ast: a regex fires on both of these.
    for code in (
        'note = "remember to os.remove(old) by hand"',
        'sql_docs = "DROP is how you remove a table"',
        'help_text = "eval() is dangerous"',
    ):
        assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN, code


def test_an_empty_cell_is_clean():
    assert codeguard.scan_code("").band == codeguard.BAND_CLEAN
    assert codeguard.scan_code("   \n\n").band == codeguard.BAND_CLEAN


# ---------------------------------------------------------------------------
# The detector table — one positive case per kind
# ---------------------------------------------------------------------------

# kind -> a cell that must produce it. Used both as a per-kind test and (below) as the
# proof that no kind in KINDS is unreachable.
POSITIVES = {
    "deletes_files": "os.remove(old)",
    "destroys_rows": 'con.execute("DROP TABLE sales")',
    "runs_program": 'subprocess.run(["ls"], check=True)',
    "dynamic_code": "value = eval(expr)",
    "edits_mooring_config": 'open("mooring.toml", "w").write(text)',
    "overwrites_file": 'df.to_csv("out.csv")',
    "changes_database": 'con.execute("INSERT INTO sales VALUES (1)")',
    "sends_data": "requests.post(url, json=payload)",
    "installs_package": 'command = "pip install polars"',
    "unparseable": "df = (",
}


@pytest.mark.parametrize("kind,code", sorted(POSITIVES.items()))
def test_each_kind_fires(kind, code):
    verdict = codeguard.scan_code(code)
    assert kind in {f.kind for f in verdict.findings}, codeguard.describe(verdict)
    assert verdict.band == codeguard.KINDS[kind][1]


def test_every_kind_in_the_table_is_reachable():
    fired = {f.kind for code in POSITIVES.values() for f in codeguard.scan_code(code).findings}
    fired |= {f.kind for f in codeguard.scan_ops([{"op": "replace_all", "cells": []}]).findings}
    assert fired == set(codeguard.KINDS)


@pytest.mark.parametrize(
    "code",
    [
        "os.remove(old)",
        "os.unlink(old)",
        "os.rmdir(tmp)",
        "os.removedirs(tmp)",
        "shutil.rmtree(tmp)",
        "p.unlink()",
        'Path(tmp).rmdir()',
        "from os import remove\nremove(old)",
        "import shutil as sh\nsh.rmtree(tmp)",
    ],
)
def test_deletes_files_is_a_floor(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_FLOOR
    assert {f.kind for f in verdict.findings} == {"deletes_files"}


@pytest.mark.parametrize(
    "code",
    [
        'subprocess.run(["ls"])',
        "subprocess.Popen(cmd)",
        "subprocess.check_output(cmd)",
        'os.system("dir")',
        'os.popen("dir")',
        'os.startfile("report.xlsx")',
        "os.execv(prog, args)",
        "os.spawnl(mode, prog)",
        "from subprocess import run\nrun(cmd)",
    ],
)
def test_runs_program_is_a_floor(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_FLOOR
    assert "runs_program" in {f.kind for f in verdict.findings}


@pytest.mark.parametrize(
    "code",
    [
        "value = eval(expr)",
        "exec(source)",
        "code = compile(src, name, mode)",
        '__import__(module_name)',
        "obj = pickle.loads(blob)",
        "obj = marshal.loads(blob)",
        "getattr(os, action)()",
    ],
)
def test_dynamic_code_is_a_floor(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_FLOOR
    assert "dynamic_code" in {f.kind for f in verdict.findings}


def test_getattr_on_a_dataframe_is_not_dynamic_code():
    # The narrow rule that keeps everyday reflection out of the floor band.
    assert codeguard.scan_code("col = getattr(df, name)").band == codeguard.BAND_CLEAN
    assert codeguard.scan_code('fn = getattr(os, "getcwd")').band == codeguard.BAND_CLEAN


@pytest.mark.parametrize(
    "code",
    [
        'open("mooring.toml", "w").write(text)',
        'Path(".marimo.toml").write_text(cfg)',
        'df.to_csv(".mooring/manifest.json")',
        'Path(".mooring") / "state.json"\nopen(".mooring/state.json", "w")',
    ],
)
def test_writing_mooring_config_is_a_floor(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_FLOOR
    assert "edits_mooring_config" in {f.kind for f in verdict.findings}


@pytest.mark.parametrize(
    "code",
    [
        'df.to_csv("out.csv")',
        'df.to_excel("out.xlsx")',
        'df.to_parquet("out.parquet")',
        'df.write_parquet("out.parquet")',
        'df.write_excel(workbook="out.xlsx")',
        'fig.savefig("chart.png")',
        'Path("notes.md").write_text(body)',
        'Path("notes.bin").write_bytes(blob)',
        'open("report.txt", "w")',
        'open("report.txt", "a")',
        'p.open("w")',
        "df.to_csv(out_path)",
        'df.to_csv(f"out_{today}.csv")',
        'gzip.open("out.gz", "wb")',
    ],
)
def test_overwrites_file_asks(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_ASK
    assert "overwrites_file" in {f.kind for f in verdict.findings}


@pytest.mark.parametrize(
    "code",
    [
        'df.to_csv(".mooring/outbox/report.csv")',
        'df.to_csv("./.mooring/outbox/report.csv")',
        'df.to_csv(".mooring\\\\outbox\\\\report.csv")',
        'open(".mooring/outbox/report.html", "w")',
        'Path(".mooring/outbox") / "report.csv"\nopen(".mooring/outbox/report.csv", "w")',
        'fig.savefig(Path(".mooring/outbox", "chart.png"))',
    ],
)
def test_a_new_file_in_the_outbox_stays_clean(code):
    assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN


@pytest.mark.parametrize(
    "code,expected",
    [
        # The carve-out is the one rule that turns a write clean, so it must not be
        # reachable by navigating out of the drop box.
        ('open(".mooring/outbox/../mooring.toml", "w")', "edits_mooring_config"),
        ('open(".mooring/outbox/../../.mooring/manifest.json", "w")', "edits_mooring_config"),
        ('df.to_csv(".mooring/outbox/../../notebooks/monthly.py")', "overwrites_file"),
        ('df.to_csv(".mooring/outbox/../../../etc/passwd")', "overwrites_file"),
        # Windows is the primary platform: the backslash spelling must behave identically.
        ('open(".mooring\\\\outbox\\\\..\\\\mooring.toml", "w")', "edits_mooring_config"),
        ('df.to_csv(".mooring\\\\outbox\\\\..\\\\..\\\\report.csv")', "overwrites_file"),
        # An absolute path names nobody's outbox in particular.
        ('df.to_csv("C:/x/.mooring/outbox/r.csv")', "edits_mooring_config"),
        ('df.to_csv("/srv/data/report.csv")', "overwrites_file"),
        # A config name anywhere still outranks everything.
        ('df.to_csv(".mooring/outbox/mooring.toml")', "edits_mooring_config"),
    ],
)
def test_the_outbox_carve_out_cannot_be_escaped(code, expected):
    verdict = codeguard.scan_code(code)
    assert {f.kind for f in verdict.findings} == {expected}, codeguard.describe(verdict)


def test_the_two_config_checks_sit_on_opposite_sides_of_the_carve_out():
    """LOAD-BEARING — this asymmetry looks like something to tidy, and is not.

    The config NAME check runs BEFORE the outbox carve-out (a delivered artifact is never
    called mooring.toml, so it costs nothing and closes every route to clean), while the
    `.mooring/` DIRECTORY check runs AFTER it (every Deliver artifact sits under a
    `.mooring/` segment, so running it first would prompt on all of them). Collapsing
    them into one condition breaks whichever side it lands on — one of these two
    assertions fails either way."""
    inside = codeguard.scan_code('df.to_csv(".mooring/outbox/report.csv")')
    assert inside.band == codeguard.BAND_CLEAN, "dir check must NOT precede the carve-out"
    named = codeguard.scan_code('df.to_csv(".mooring/outbox/mooring.toml")')
    assert {f.kind for f in named.findings} == {"edits_mooring_config"}, (
        "name check MUST precede the carve-out"
    )


def test_the_outbox_carve_out_still_covers_real_deliveries():
    for code in (
        'df.to_csv(".mooring/outbox/report.csv")',
        'df.to_csv("./.mooring/outbox/report.csv")',
        'df.to_csv(".mooring/outbox/2026-08/report.csv")',
        'open(".mooring\\\\outbox\\\\report.html", "w")',
        # a `..` that collapses back inside the box IS still inside it
        'df.to_csv(".mooring/outbox/sub/../report.csv")',
    ):
        assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN, code


@pytest.mark.parametrize(
    "code",
    [
        "builtins.exec(source)",
        "builtins.eval(expr)",
        "builtins.compile(src, name, mode)",
        "builtins.__import__(module_name)",
        'getattr(builtins, "exec")(source)',
    ],
)
def test_builtins_is_not_a_way_around_the_dynamic_code_floor(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_FLOOR
    assert "dynamic_code" in {f.kind for f in verdict.findings}


@pytest.mark.parametrize(
    "code,expected",
    [
        # A literal getattr is not dynamic — but it IS the attribute access it spells,
        # and must be classified as one.
        ('getattr(os, "remove")(path)', "deletes_files"),
        ('getattr(os, "system")(cmd)', "runs_program"),
        ('getattr(shutil, "rmtree")(tmp)', "deletes_files"),
        ('getattr(os, "execv")(prog, args)', "runs_program"),
        ('getattr(subprocess, "run")(cmd)', "runs_program"),
        ('handler = getattr(os, "remove")', "deletes_files"),
    ],
)
def test_a_literal_getattr_is_classified_as_the_call_it_spells(code, expected):
    verdict = codeguard.scan_code(code)
    assert {f.kind for f in verdict.findings} == {expected}


def test_a_harmless_literal_getattr_stays_clean():
    for code in ('fn = getattr(os, "getcwd")', 'p = getattr(os, "sep")', "col = getattr(df, name)"):
        assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN, code


@pytest.mark.parametrize(
    "code",
    [
        # Receivers this module has never heard of, all taking the path FIRST. Reading
        # them as pathlib would take "report.csv" for a mode and pass them silently.
        'fs.open("report.csv", "w")',
        'zf.open("member.txt", "w")',
        'smart_open.open("s3://bucket/key", "wb")',
        'store.open(path, mode="a")',
        'handle.open(target, "x")',
    ],
)
def test_an_unknown_open_receiver_fails_toward_ask(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_ASK
    assert "overwrites_file" in {f.kind for f in verdict.findings}


def test_an_unknown_open_receiver_still_reads_cleanly():
    for code in ("p.open()", 'p.open("r")', 'fs.open("report.csv")', 'fs.open(path, "rb")'):
        assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN, code


def test_scan_code_never_raises_even_when_a_helper_does(monkeypatch):
    """The guard must fail CLOSED. A crash here would be a 500 on the Apply route."""

    def boom(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(codeguard, "_prose_nodes", boom)
    verdict = codeguard.scan_code("df = pd.read_csv('sales.csv')")
    assert verdict.band == codeguard.BAND_ASK
    assert [f.kind for f in verdict.findings] == ["unparseable"]

    monkeypatch.setattr(codeguard, "_buffer_binds", boom)
    assert codeguard.scan_ops([{"op": "append", "code": "x = 1"}]).band == codeguard.BAND_ASK


def test_a_pathologically_nested_cell_does_not_crash():
    deep = "p = " + "Path(" * 400 + '"a"' + ")" * 400
    assert codeguard.scan_code(deep).band in (codeguard.BAND_ASK, codeguard.BAND_CLEAN)


@pytest.mark.parametrize(
    "code",
    [
        'button_text = "Delete from list"',
        'options = ["Delete from list", "Keep all rows"]',
        'action = "Drop table view"',
        'sql = "DROP TABLE sales"',
        'query = "DELETE FROM sales"',
        'step = "TRUNCATE TABLE staging"',
    ],
)
def test_a_loose_literal_never_reaches_the_floor(code):
    # Only SQL in a known SQL slot may reach the band nothing can downgrade — no
    # english-detection heuristic can win that arms race.
    verdict = codeguard.scan_code(code)
    assert verdict.band != codeguard.BAND_FLOOR
    assert "destroys_rows" not in {f.kind for f in verdict.findings}


def test_a_loose_literal_handed_to_a_cursor_recovers_the_floor():
    for code in (
        'query = "DELETE FROM sales"\ncon.execute(query)',
        'sql = "DROP TABLE staging"\ncur.execute(sql)',
        'q = """\n  DELETE FROM sales\n"""\nresult = mo.sql(q)',
    ):
        verdict = codeguard.scan_code(code)
        assert verdict.band == codeguard.BAND_FLOOR, code
        assert "destroys_rows" in {f.kind for f in verdict.findings}


def test_the_sql_stripper_fails_closed_on_an_unbalanced_quote():
    # Text after a quote that never closes is discarded rather than scanned, so trailing
    # prose can never supply the keywords.
    assert codeguard.scan_code('note = "Don\'t DROP TABLE without a backup"').findings == ()
    assert codeguard.scan_code('note = "It\'s time to TRUNCATE TABLE staging"').findings == ()
    # The cut is per STATEMENT, so one malformed statement cannot hide the next — this
    # module must not copy ast_walk's whole-string truncation, where losing text is safe.
    verdict = codeguard.scan_code(
        "con.execute(\"SELECT * FROM t WHERE note = 'x; DROP TABLE sales\")"
    )
    assert {f.kind for f in verdict.findings} == {"destroys_rows"}


@pytest.mark.parametrize(
    "code,expected",
    [
        ("buf = io.StringIO()\ndf.write_csv(buf)", set()),
        ("with io.StringIO() as buf:\n    df.write_csv(buf)", set()),
        # rebound to a real path before the write
        ('buf = io.StringIO()\nbuf = "sales.csv"\ndf.write_csv(buf)', {"overwrites_file"}),
        # the write happens before the binding
        ("df.write_csv(buf)\nbuf = io.StringIO()", {"overwrites_file"}),
        # bound by a loop, not by a buffer factory
        ("for buf in paths:\n    df.write_csv(buf)", {"overwrites_file"}),
    ],
)
def test_the_buffer_carve_out_is_positional(code, expected):
    assert {f.kind for f in codeguard.scan_code(code).findings} == expected


def test_token_binds_the_notebook_bytes():
    ops = [{"op": "append", "code": "os.remove(old)"}]
    verdict = codeguard.scan_ops(ops)
    before = codeguard.token("notebooks/a.py", ops, verdict, notebook_bytes=b"import marimo\n")
    after = codeguard.token("notebooks/a.py", ops, verdict, notebook_bytes=b"import marimo\nx=1\n")
    assert before != after
    # An append carries no anchor, so the bytes are the only thing that notices drift.
    assert before == codeguard.token(
        "notebooks/a.py", ops, verdict, notebook_bytes=b"import marimo\n"
    )


SIX_SQL_CALL_SITES = [
    # Every shape the loose-literal ceiling would otherwise have downgraded to clean.
    'q = "DROP TABLE t"\ncon.execute(q)',  # one-hop local binding
    'con.execute(text("DROP TABLE t"))',  # sqlalchemy's standard idiom
    'cur.execute(conn, "DROP TABLE t")',  # SQL as the 2nd positional
    'pl.read_database("DROP TABLE t", conn)',  # polars
    'engine.exec_driver_sql("DROP TABLE t")',
    'con.execute(textwrap.dedent("DROP TABLE t"))',
]


@pytest.mark.parametrize("code", SIX_SQL_CALL_SITES)
def test_a_known_sql_slot_reaches_the_floor_through_every_shape(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_FLOOR, codeguard.describe(verdict)
    assert "destroys_rows" in {f.kind for f in verdict.findings}


def test_a_bound_query_is_reported_once_at_the_query():
    # The call site reports at the RESOLVED line, so the analyst gets one reason
    # pointing at the query rather than two saying the same thing.
    verdict = codeguard.scan_code('query = "DROP TABLE sales"\ncon.execute(query)')
    assert [(f.line, f.kind) for f in verdict.findings] == [(1, "destroys_rows")]


def test_a_rebound_query_name_resolves_through_every_binding():
    code = (
        'query = "SELECT 1"\n'
        'if full:\n'
        '    query = "DROP TABLE sales"\n'
        'con.execute(query)\n'
    )
    assert codeguard.scan_code(code).band == codeguard.BAND_FLOOR


def test_a_parameter_beside_a_query_is_not_read_as_sql():
    for code in (
        'cur.execute("SELECT * FROM sales WHERE id = ?", ident)',
        'pd.read_sql("SELECT 1", con)',
    ):
        assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN, code
    # A VALUE in the parameter slot is data, not a statement: the call site does not
    # vouch for it, so the loose-literal ceiling still applies to it.
    verdict = codeguard.scan_code('con.execute("SELECT 1", ["DROP TABLE sales"])')
    assert verdict.band != codeguard.BAND_FLOOR


@pytest.mark.parametrize(
    "sql,expected",
    [
        # DuckDB's own verbs — this repo ships "copilot Speak SQL" on DuckDB, so a model
        # writes these, and COPY … TO reaches the disk without any Python write call.
        ("COPY (SELECT * FROM df) TO 'out.parquet'", "overwrites_file"),
        ("COPY sales FROM 'in.csv'", "changes_database"),
        ("INSTALL httpfs", "installs_package"),
        ("FORCE INSTALL spatial", "installs_package"),
        ("ATTACH 'other.db' AS o", "changes_database"),
        ("SELECT * FROM read_parquet('in.parquet')", None),
    ],
)
def test_duckdb_verbs(sql, expected):
    verdict = codeguard.scan_code(f"con.execute({sql!r})")
    assert {f.kind for f in verdict.findings} == (set() if expected is None else {expected})


@pytest.mark.parametrize(
    "code,expected",
    [
        ("os.replace(tmp, final)", {"deletes_files"}),
        ("os.truncate(path, 0)", {"deletes_files"}),
        ("os.rename(old, new)", {"deletes_files"}),
        ("shutil.move(src, dst)", {"deletes_files"}),
        ("shutil.copy(src, dst)", {"overwrites_file"}),
        ("shutil.copy2(src, dst)", {"overwrites_file"}),
        ("shutil.copytree(a, b, dirs_exist_ok=True)", {"overwrites_file"}),
        ("shutil.copytree(a, b)", set()),  # a fresh tree creates, it destroys nothing
        ('np.save("arr.npy", values)', {"overwrites_file"}),
        ('np.savez("bundle.npz", a=x)', {"overwrites_file"}),
        ('joblib.dump(model, "model.pkl")', {"overwrites_file"}),
        ('torch.save(model, "model.pt")', {"overwrites_file"}),
        ('wb.save("book.xlsx")', {"overwrites_file"}),
        ('chart.save("chart.png")', {"overwrites_file"}),
        ("pickle.dump(obj, fh)", {"overwrites_file"}),
        ("text = yaml.dump(data)", set()),  # one argument renders to a string
        ('session.request("POST", url, json=body)', {"sends_data"}),
    ],
)
def test_destructive_and_artifact_writers(code, expected):
    assert {f.kind for f in codeguard.scan_code(code).findings} == expected


def test_pandas_rename_and_replace_are_never_flagged():
    """LOAD-BEARING — do not delete to make room for a `Path.rename` detector.

    The one failure this gate cannot survive is an un-downgradable floor prompt on
    ordinary dataframe work, and a bare `.rename`/`.replace` has an unresolvable
    receiver. `os.rename`/`os.replace`/`shutil.move` are caught by the module table;
    `p.rename(q)` is a deliberate, documented miss and this test is what records it."""
    for code in (
        'df = df.rename(columns={"a": "b"})',
        "df = df.replace(0, None)",
        's = s.replace("x", "y")',
        "df = df.rename(str.lower, axis=1)",
    ):
        assert codeguard.scan_code(code).band == codeguard.BAND_CLEAN, code


@pytest.mark.parametrize(
    "code,fires",
    [
        ('command = "pip install polars"', True),
        ('command = "python -m pip install polars"', True),
        ('command = "C:/py/python.exe -m pip install polars"', True),
        ('args = [sys.executable, "-m", "pip", "install", "polars"]', True),
        ('os.system("cd /tmp && pip install polars")', True),
        ('cmd = "uv add polars"', True),
        # a setup NOTE mentions the command; it is not one
        ('setup_note = "Run pip install polars first"', False),
        ('doc = "You may need to uv add polars before this works"', False),
        ('hint = "we recommend conda install polars for windows"', False),
    ],
)
def test_install_detection_is_anchored_to_a_command(code, fires):
    kinds = {f.kind for f in codeguard.scan_code(code).findings}
    assert ("installs_package" in kinds) is fires, kinds


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "replace_all", "cells": 7}],
        [{"op": "replace_all", "cells": None}],
        [{"op": "append", "code": 7}],
        [{"op": "append", "code": None}],
        [{"op": "edit", "index": "x", "anchor": None, "code": object()}],
        [None, 3, "x"],
        7,
        "not a list",
        {"op": "append"},
    ],
)
def test_scan_ops_never_raises(ops):
    """The gate runs BEFORE cellwrite can reject a malformed op, so a shape it cannot
    read has to become a held Apply, never a 500."""
    verdict = codeguard.scan_ops(ops)
    assert verdict.band in (codeguard.BAND_CLEAN, codeguard.BAND_ASK, codeguard.BAND_FLOOR)


def test_a_malformed_replace_all_is_held_not_dropped():
    verdict = codeguard.scan_ops([{"op": "replace_all", "cells": 7}])
    assert verdict.band == codeguard.BAND_ASK
    assert {f.kind for f in verdict.findings} == {"replaces_notebook", "unparseable"}


def test_scan_ops_stringifies_code_the_way_cellwrite_does():
    # The gate must read what will actually be written, not a tidied version of it.
    assert codeguard.scan_ops([{"op": "append", "code": 7}]).band == codeguard.BAND_CLEAN
    assert codeguard.scan_ops([{"op": "append"}]).band == codeguard.BAND_CLEAN


def test_an_unrelated_name_is_not_a_buffer():
    # The buffer carve-out is bound to the assignment that created it, in this cell.
    verdict = codeguard.scan_code("buf = out_dir / name\ndf.write_csv(buf)")
    assert {f.kind for f in verdict.findings} == {"overwrites_file"}


def test_a_computed_outbox_path_still_asks():
    # The carve-out is only ever granted to a path the gate could actually read.
    code = 'df.to_csv(outbox / name)'
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_ASK
    assert {f.kind for f in verdict.findings} == {"overwrites_file"}


@pytest.mark.parametrize(
    "code",
    [
        "requests.post(url, json=payload)",
        "requests.put(url, data=body)",
        "requests.patch(url, data=body)",
        "requests.delete(url)",
        "httpx.post(url, content=body)",
        "session.post(url, data=body)",
        "urlopen(request, body)",
        "urlopen(request, data=body)",
        "smtplib.SMTP(host)",
        "server.sendmail(sender, to, message)",
        "client.put_object(Bucket=bucket, Key=key, Body=body)",
        "client.upload_file(path, bucket, key)",
    ],
)
def test_sends_data_asks(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_ASK
    assert "sends_data" in {f.kind for f in verdict.findings}


@pytest.mark.parametrize(
    "code",
    [
        'command = "pip install polars"',
        'command = "python -m pip install polars"',
        'command = "uv add polars"',
        'command = "uv pip install polars"',
        'command = "conda install polars"',
        'args = [sys.executable, "-m", "pip", "install", "polars"]',
        "micropip.install(name)",
    ],
)
def test_installs_package_asks(code):
    verdict = codeguard.scan_code(code)
    assert verdict.band == codeguard.BAND_ASK
    assert "installs_package" in {f.kind for f in verdict.findings}


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM sales", None),
        ("WITH recent AS (SELECT 1) SELECT * FROM recent", None),
        ("EXPLAIN SELECT * FROM sales", None),
        ("CREATE TABLE staging AS SELECT * FROM sales", None),
        ("DROP TABLE sales", "destroys_rows"),
        ("TRUNCATE TABLE staging", "destroys_rows"),
        ("DELETE FROM sales", "destroys_rows"),
        ("delete from sales", "destroys_rows"),
        ("DELETE FROM sales WHERE id = 1", "changes_database"),
        ("INSERT INTO sales VALUES (1)", "changes_database"),
        ("UPDATE sales SET amount = 0", "changes_database"),
        ("ALTER TABLE sales ADD COLUMN x INT", "changes_database"),
        ("MERGE INTO sales USING staging ON sales.id = staging.id", "changes_database"),
        ("CREATE OR REPLACE VIEW v AS SELECT 1", "changes_database"),
        ("GRANT SELECT ON sales TO analyst", "changes_database"),
        ("WITH recent AS (SELECT 1) DELETE FROM sales USING recent", "destroys_rows"),
    ],
)
def test_sql_first_keyword_classification(sql, expected):
    verdict = codeguard.scan_code(f"con.execute({sql!r})")
    kinds = {f.kind for f in verdict.findings}
    assert kinds == (set() if expected is None else {expected}), sql


def test_sql_reaches_the_scanner_through_every_call_site():
    for call in (
        'mo.sql("DELETE FROM sales")',
        'con.execute("DELETE FROM sales")',
        'cur.executemany("DELETE FROM sales", rows)',
        'pd.read_sql("DELETE FROM sales", con)',
        'pd.read_sql_query("DELETE FROM sales", con)',
        # a loose literal handed to a cursor in the same cell: the call site vouches
        # for it, so it recovers the floor its own shape could not reach.
        'query = "DELETE FROM sales"\ncon.execute(query)',
    ):
        verdict = codeguard.scan_code(call)
        assert verdict.band == codeguard.BAND_FLOOR, call


def test_an_fstring_query_is_read_through_its_literal_parts():
    verdict = codeguard.scan_code('mo.sql(f"DELETE FROM {table}")')
    assert {f.kind for f in verdict.findings} == {"destroys_rows"}


def test_a_value_inside_the_sql_cannot_steer_the_classification():
    # The quoted span is stripped before the keyword scan, so neither the ';' nor the
    # 'WHERE' inside a literal changes the verdict.
    verdict = codeguard.scan_code(
        "con.execute(\"DELETE FROM notes WHERE body = 'ok; WHERE'\")"
    )
    assert {f.kind for f in verdict.findings} == {"changes_database"}
    verdict = codeguard.scan_code("con.execute(\"DELETE FROM notes -- WHERE id = 1\")")
    assert {f.kind for f in verdict.findings} == {"destroys_rows"}


def test_multiple_statements_each_classify():
    verdict = codeguard.scan_code(
        'con.execute("SELECT 1; UPDATE sales SET x = 1; DROP TABLE staging")'
    )
    assert {f.kind for f in verdict.findings} == {"changes_database", "destroys_rows"}
    assert verdict.band == codeguard.BAND_FLOOR


def test_to_sql_always_changes_the_database():
    for code in ('df.to_sql("sales", con)', 'df.to_sql("sales", con, if_exists="append")'):
        verdict = codeguard.scan_code(code)
        assert {f.kind for f in verdict.findings} == {"changes_database"}


# ---------------------------------------------------------------------------
# Bands, ordering, describe
# ---------------------------------------------------------------------------


def test_worst_band_wins():
    code = 'df.to_csv("out.csv")\nos.remove(old)\n'
    verdict = codeguard.scan_code(code)
    assert {f.kind for f in verdict.findings} == {"overwrites_file", "deletes_files"}
    assert verdict.band == codeguard.BAND_FLOOR


def test_a_floor_finding_cannot_be_diluted_by_asks():
    code = 'os.system("uv add polars")\n'
    verdict = codeguard.scan_code(code)
    assert {f.kind for f in verdict.findings} == {"runs_program", "installs_package"}
    assert verdict.band == codeguard.BAND_FLOOR


def test_findings_are_sorted_and_deduped():
    code = 'df.to_csv("a.csv")\ndf.to_csv("b.csv")\nos.remove(old)\nos.remove(other)\n'
    verdict = codeguard.scan_code(code)
    assert [(f.line, f.kind) for f in verdict.findings] == [
        (1, "overwrites_file"),
        (2, "overwrites_file"),
        (3, "deletes_files"),
        (4, "deletes_files"),
    ]
    # requests.post matches a module rule AND an attribute rule: one reason, once.
    once = codeguard.scan_code("requests.post(url, json=payload)")
    assert len(once.findings) == 1


def test_line_numbers_point_at_the_call():
    code = "x = 1\n\ny = 2\nos.remove(old)\n"
    (finding,) = codeguard.scan_code(code).findings
    assert finding.line == 4


def test_describe_is_a_value_free_one_liner():
    verdict = codeguard.scan_code("x = 1\nos.remove(old)\n")
    assert codeguard.describe(verdict) == ["line 2: Deletes files or folders"]


def test_labels_are_fixed_strings_from_the_table():
    for kind, (label, band) in codeguard.KINDS.items():
        assert band in (codeguard.BAND_ASK, codeguard.BAND_FLOOR), kind
        assert label == label.strip() and label
    assert codeguard.BAND_CLEAN not in {band for _, band in codeguard.KINDS.values()}


# ---------------------------------------------------------------------------
# Value-freedom
# ---------------------------------------------------------------------------


def test_findings_are_value_free():
    """A secret planted in code that DOES trip findings must appear nowhere downstream."""
    code = (
        f'token = "{SECRET_VALUE_DO_NOT_LEAK}"\n'
        f'os.remove("/data/{SECRET_VALUE_DO_NOT_LEAK}.csv")\n'
        f'con.execute("DELETE FROM {SECRET_VALUE_DO_NOT_LEAK}")\n'
        f'open("{SECRET_VALUE_DO_NOT_LEAK}.txt", "w")\n'
        f'requests.post("https://example.com/{SECRET_VALUE_DO_NOT_LEAK}", json=payload)\n'
    )
    ops = [{"op": "append", "code": code}]
    verdict = codeguard.scan_ops(ops)
    assert verdict.band == codeguard.BAND_FLOOR
    assert len(verdict.findings) >= 4
    blob = repr(verdict) + "\n".join(codeguard.describe(verdict))
    blob += codeguard.token("notebooks/a.py", ops, verdict, notebook_bytes=code.encode())
    assert SECRET_VALUE_DO_NOT_LEAK not in blob
    for finding in verdict.findings:
        assert finding.label == codeguard.KINDS[finding.kind][0]


def test_an_unparseable_cell_never_crashes():
    for code in ("df = (", "def helper(\n", "if True\n    pass", "\x00\x01"):
        verdict = codeguard.scan_code(code)
        assert verdict.band == codeguard.BAND_ASK
        assert [f.kind for f in verdict.findings] == ["unparseable"]
        assert verdict.findings[0].line == 1


# ---------------------------------------------------------------------------
# scan_ops — the wire shapes
# ---------------------------------------------------------------------------


def test_scan_ops_unions_append_and_edit_code():
    ops = [
        {"op": "append", "code": 'df.to_csv("out.csv")'},
        {"op": "edit", "index": 2, "anchor": "x = 1", "code": "os.remove(old)"},
    ]
    verdict = codeguard.scan_ops(ops)
    assert {f.kind for f in verdict.findings} == {"overwrites_file", "deletes_files"}
    assert verdict.band == codeguard.BAND_FLOOR


def test_scan_ops_never_scans_the_code_being_removed():
    # An anchor is the cell's EXISTING source, and a delete introduces nothing: flagging
    # either would prompt for taking dangerous code away.
    ops = [
        {"op": "delete", "index": 2, "anchor": "shutil.rmtree(tmp)"},
        {"op": "edit", "index": 3, "anchor": "os.remove(old)", "code": "x = 1"},
    ]
    verdict = codeguard.scan_ops(ops)
    assert verdict.band == codeguard.BAND_CLEAN
    assert verdict.findings == ()


def test_replace_all_always_replaces_the_notebook():
    clean = codeguard.scan_ops([{"op": "replace_all", "cells": ["x = 1", "y = 2"]}])
    assert [f.kind for f in clean.findings] == ["replaces_notebook"]
    assert clean.band == codeguard.BAND_ASK

    dirty = codeguard.scan_ops([{"op": "replace_all", "cells": ["x = 1", "os.remove(p)"]}])
    assert {f.kind for f in dirty.findings} == {"replaces_notebook", "deletes_files"}
    assert dirty.band == codeguard.BAND_FLOOR


def test_scan_ops_tolerates_junk():
    assert codeguard.scan_ops([]).band == codeguard.BAND_CLEAN
    assert codeguard.scan_ops(None).band == codeguard.BAND_CLEAN
    assert codeguard.scan_ops(["not a dict", 7, None]).band == codeguard.BAND_CLEAN
    # An op shape cellwrite would reject outright never reaches a write, so it is not
    # the gate's job to hold it.
    assert codeguard.scan_ops([{"op": "teleport", "code": "os.remove(p)"}]).findings == ()


def test_an_unparseable_op_is_carried_through_scan_ops():
    verdict = codeguard.scan_ops([{"op": "append", "code": "df = ("}])
    assert [f.kind for f in verdict.findings] == ["unparseable"]
    assert verdict.band == codeguard.BAND_ASK


# ---------------------------------------------------------------------------
# The confirm token
# ---------------------------------------------------------------------------


NOTEBOOK = b"import marimo\n\napp = marimo.App()\n"


def _token(rel, ops, notebook=NOTEBOOK):
    return codeguard.token(rel, ops, codeguard.scan_ops(ops), notebook_bytes=notebook)


def test_token_is_stable_for_the_same_notebook_ops_and_findings():
    ops = [{"op": "append", "code": "os.remove(old)"}]
    first = _token("notebooks/a.py", ops)
    assert first == _token("notebooks/a.py", ops)
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


def test_token_ignores_the_order_of_the_wire_keys():
    ordered = [{"op": "append", "code": "os.remove(old)"}]
    shuffled = [{"code": "os.remove(old)", "op": "append"}]
    assert _token("notebooks/a.py", ordered) == _token("notebooks/a.py", shuffled)


def test_token_changes_when_the_code_changes():
    base = _token("notebooks/a.py", [{"op": "append", "code": "os.remove(old)"}])
    assert base != _token("notebooks/a.py", [{"op": "append", "code": "os.remove(other)"}])
    # even a change that leaves the findings identical must mint a new token
    assert base != _token("notebooks/a.py", [{"op": "append", "code": "os.remove(old) "}])


def test_token_changes_when_the_notebook_changes():
    ops = [{"op": "append", "code": "os.remove(old)"}]
    assert _token("notebooks/a.py", ops) != _token("notebooks/b.py", ops)


def test_token_changes_when_the_findings_change():
    ops = [{"op": "append", "code": "os.remove(old)"}]
    verdict = codeguard.scan_ops(ops)
    extra = codeguard.Verdict(
        band=verdict.band,
        findings=verdict.findings + (
            codeguard.Finding(
                line=9,
                kind="sends_data",
                label=codeguard.KINDS["sends_data"][0],
                band=codeguard.BAND_ASK,
            ),
        ),
    )
    assert codeguard.token(
        "notebooks/a.py", ops, verdict, notebook_bytes=NOTEBOOK
    ) != codeguard.token("notebooks/a.py", ops, extra, notebook_bytes=NOTEBOOK)


def test_token_changes_when_an_anchor_changes():
    # The anchor is not scanned, but it decides WHICH cell the patch lands on, so the
    # token has to bind it.
    a = [{"op": "edit", "index": 1, "anchor": "x = 1", "code": "y = 2"}]
    b = [{"op": "edit", "index": 1, "anchor": "x = 2", "code": "y = 2"}]
    assert _token("notebooks/a.py", a) != _token("notebooks/a.py", b)
