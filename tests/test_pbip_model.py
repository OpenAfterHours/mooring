"""The PBIP semantic-model extractor's allowlist is the whole game: partition M
bodies are skipped uncaptured, roles/translations are never opened, annotations
and unknown constructs are dropped, and a parse failure fails soft. Pinned with
the SECRET_VALUE_DO_NOT_LEAK idiom from test_schema/test_ai_dict_tools."""

from __future__ import annotations

from mooring import pbip_model

SECRET = "SECRET_VALUE_DO_NOT_LEAK"
VALID_CARD = "4012888888881881"  # Luhn-valid, not on any test-PAN list (test_egress idiom)


def _write(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")


def _model_tree(ws, key="reports/Sales", *, measure_dax="SUM(Sales[Amount])"):
    """A realistic Desktop-shaped TMDL tree with the sentinel planted in every
    construct the allowlist must exclude: a partition M connection string, an RLS
    role filter, an annotation, and a translation."""
    base = f"{key}.SemanticModel"
    _write(ws, f"{key}.pbip", "{}")
    _write(
        ws,
        f"{base}/definition/tables/Sales.tmdl",
        "table Sales\n"
        "\tlineageTag: 11111111-aaaa\n"
        "\tisHidden\n"
        "\n"
        f"\tmeasure 'Gross Margin %' = {measure_dax}\n"
        "\t\tformatString: 0.00%\n"
        "\t\tdisplayFolder: Margins\n"
        "\t\tlineageTag: 22222222-bbbb\n"
        "\n"
        f'\t\tannotation PBI_FormatHint = {{"secret":"{SECRET}"}}\n'
        "\n"
        "\tmeasure 'Total Sales' =\n"
        "\t\t\tCALCULATE(\n"
        "\t\t\t    SUM(Sales[Amount])\n"
        "\t\t\t)\n"
        "\t\tformatString: #,0\n"
        "\n"
        "\tcolumn Amount\n"
        "\t\tdataType: decimal\n"
        "\t\tsourceColumn: Amount\n"
        "\t\tsummarizeBy: sum\n"
        "\n"
        "\t\tannotation SummarizationSetBy = Automatic\n"
        "\n"
        "\tcolumn Margin = [Amount] - [Cost]\n"
        "\t\tdataType: decimal\n"
        "\t\ttype: calculated\n"
        "\n"
        "\thierarchy 'Date Hierarchy'\n"
        "\t\tlevel Year\n"
        "\t\t\tcolumn: Year\n"
        "\n"
        "\tpartition Sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        f'\t\t\t\t    Source = Sql.Database("{SECRET}", "db")\n'
        "\t\t\t\tin\n"
        "\t\t\t\t    Source\n"
        "\n"
        "\tannotation PBI_ResultType = Table\n",
    )
    _write(
        ws,
        f"{base}/definition/relationships.tmdl",
        "relationship 1aaa\n"
        "\tfromColumn: Sales.DateKey\n"
        "\ttoColumn: Date.DateKey\n"
        "\n"
        "relationship 2bbb\n"
        "\ttoCardinality: many\n"
        "\tfromColumn: Sales.Region\n"
        "\ttoColumn: Region.Region\n",
    )
    # RLS role filter + translation: the sentinel lives in files the extractor
    # must NEVER OPEN (allowlist means the bytes are not read, not read-then-dropped).
    _write(
        ws,
        f"{base}/definition/roles/Restricted.tmdl",
        f"role Restricted\n\ttablePermission Sales = Sales[Region] = \"{SECRET}\"\n",
    )
    _write(
        ws,
        f"{base}/definition/cultures/en-US.tmdl",
        f"cultureInfo en-US\n\tlinguisticMetadata = {{\"caption\": \"{SECRET}\"}}\n",
    )
    return ws / base


def _extract(tmp_path):
    model_dir = _model_tree(tmp_path)
    return pbip_model.extract_model(model_dir, key="reports/Sales")


# -- the allowlist: excluded constructs never reach ANY output -------------------


def test_sentinel_never_appears_in_any_renderer_output(tmp_path):
    model = _extract(tmp_path)
    outputs = [pbip_model.render_summary(model), pbip_model.render_models_hint([model])]
    for table in model.tables:
        outputs.append(pbip_model.render_table(model, table))
        for measure in table.measures:
            outputs.append(pbip_model.render_measure(model, table, measure))
    for out in outputs:
        assert SECRET not in out
    # Stronger: the sentinel is not captured ANYWHERE in the extracted model — the
    # partition body, role filter, annotation, and translation were never stored.
    assert SECRET not in repr(model)


def test_allowlisted_skeleton_is_kept(tmp_path):
    model = _extract(tmp_path)
    (table,) = model.tables
    assert table.name == "Sales"
    assert [c.name for c in table.columns] == ["Amount", "Margin"]
    assert table.columns[0].data_type == "decimal"
    assert table.columns[1].dax == "[Amount] - [Cost]"  # calculated-column DAX kept
    assert [m.name for m in table.measures] == ["Gross Margin %", "Total Sales"]
    gm = table.measures[0]
    assert gm.dax == "SUM(Sales[Amount])"
    assert gm.format_string == "0.00%" and gm.folder == "Margins"
    assert "CALCULATE" in table.measures[1].dax  # multi-line DAX captured
    assert [r.cardinality for r in model.relationships] == ["many-to-one", "many-to-many"]
    assert model.relationships[0].from_ref == "Sales.DateKey"


def test_roles_and_cultures_are_never_opened(tmp_path, monkeypatch):
    model_dir = _model_tree(tmp_path)
    opened: list[str] = []
    real = pbip_model.Path.read_bytes

    def spy(self):
        opened.append(self.as_posix())
        return real(self)

    monkeypatch.setattr(pbip_model.Path, "read_bytes", spy)
    model = pbip_model.extract_model(model_dir)
    assert not any("/roles/" in p or "/cultures/" in p for p in opened)
    assert model.excluded.roles_files == 1 and model.excluded.culture_files == 1
    assert model.files_read == (
        "definition/tables/Sales.tmdl",
        "definition/relationships.tmdl",
    )


def test_partitions_skipped_and_drops_counted(tmp_path):
    model = _extract(tmp_path)
    ex = model.excluded
    assert ex.partitions == 1
    dropped = dict(ex.dropped)
    assert dropped.get("annotation", 0) >= 3
    assert "hierarchy" in dropped  # unknown construct -> dropped, visible in the report
    assert "lineageTag" in dropped and "sourceColumn" in dropped


# -- the scrub floor over authored DAX -------------------------------------------


def test_checksum_card_in_measure_dax_is_dropped_by_the_egress_scrub(tmp_path):
    # Authored DAX can embed literal values; the extractor keeps it (it is code,
    # like notebook source), and the CALLERS' egress.scrub_text drops the line.
    from mooring.ai import egress

    model_dir = _model_tree(
        tmp_path, measure_dax=f'IF(Sales[card] = "{VALID_CARD}", 1, 0)'
    )
    model = pbip_model.extract_model(model_dir, key="reports/Sales")
    table = model.get_table("Sales")
    rendered = pbip_model.render_table(model, table)
    assert VALID_CARD in rendered  # the extractor itself does not scrub (L2)
    scrubbed, findings = egress.scrub_text(rendered)
    assert VALID_CARD not in scrubbed and findings
    assert "Total Sales" in scrubbed  # only the offending line is withheld


# -- tolerance: unknown dropped, failure soft, discovery gates -------------------


def test_unknown_constructs_are_ignored_not_fatal(tmp_path):
    _write(
        tmp_path,
        "m.SemanticModel/definition/tables/T.tmdl",
        "table T\n"
        "\tfutureConstruct 'from a newer Desktop'\n"
        "\t\tnested: thing\n"
        "\tcolumn A\n"
        "\t\tdataType: string\n",
    )
    model = pbip_model.extract_model(tmp_path / "m.SemanticModel")
    (table,) = model.tables
    assert [c.name for c in table.columns] == ["A"]
    assert dict(model.excluded.dropped).get("futureConstruct") == 1


def test_malformed_file_yields_empty_model_with_note_never_a_raise(tmp_path):
    target = tmp_path / "m.SemanticModel/definition/tables/T.tmdl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe\x00garbage \xff not utf-8 \x80\x81")
    model = pbip_model.extract_model(tmp_path / "m.SemanticModel")
    assert model.tables == ()
    assert any("could not read" in n for n in model.notes)


def test_extract_never_raises_even_on_a_hostile_tree(tmp_path):
    # A definition dir that is a FILE (walk/glob explode differently per OS).
    bad = tmp_path / "m.SemanticModel"
    bad.mkdir()
    (bad / "definition").write_text("not a dir", "utf-8")
    model = pbip_model.extract_model(bad)
    assert model.tables == () and model.notes  # fail-soft, visibly


def test_find_models_skips_dirs_without_a_readable_definition(tmp_path):
    _model_tree(tmp_path, key="reports/Sales")
    # A report-only project: a .SemanticModel dir exists but holds no definition.
    (tmp_path / "reports" / "Empty.SemanticModel").mkdir(parents=True)
    _write(tmp_path, "reports/Empty.pbip", "{}")
    refs = pbip_model.find_models(tmp_path, ("reports", "notebooks"))
    assert [r.key for r in refs] == ["reports/Sales"]
    assert refs[0].name == "Sales"
    assert refs[0].path == tmp_path / "reports" / "Sales.SemanticModel"


def test_find_models_only_searches_the_given_folders(tmp_path):
    _model_tree(tmp_path, key="elsewhere/Sales")
    assert pbip_model.find_models(tmp_path, ("reports",)) == []


def test_definition_signature_changes_on_edit(tmp_path):
    model_dir = _model_tree(tmp_path)
    sig1 = pbip_model.definition_signature(model_dir)
    assert sig1
    _write(tmp_path, "reports/Sales.SemanticModel/definition/tables/New.tmdl", "table New\n")
    sig2 = pbip_model.definition_signature(model_dir)
    assert sig2 != sig1
    assert pbip_model.definition_signature(tmp_path / "reports" / "Nope.SemanticModel") == ()
