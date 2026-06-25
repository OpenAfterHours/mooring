"""Dictionary parsing: flexible shapes in, fixed five-slot allowlist out."""

from __future__ import annotations

from mooring.ai import datadictionary as dd

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _write(tmp_path, rel, text):
    p = tmp_path / "context" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return p


def test_dbt_shape_columns_fk_nullable_and_drops(tmp_path):
    _write(
        tmp_path,
        "dictionaries/credit.yaml",
        f"""
version: 2
models:
  - name: fact_loans
    description: the book
    meta: {{owner: x}}
    columns:
      - name: loan_id
        data_type: bigint
        constraints: [{{type: not_null}}]
      - name: region_id
        data_type: int
        data_tests:
          - relationships: {{to: ref('dim_region'), field: region_id}}
      - name: status
        data_type: varchar
        data_tests:
          - accepted_values: {{values: ['open', 'closed', '{SECRET}']}}
""",
    )
    index = dd.load_index(tmp_path, "context")
    (report,) = index.reports
    assert report.shape == "dbt"
    assert report.n_tables == 1 and report.n_columns == 3
    assert "meta" in report.dropped_keys
    table = index.get("fact_loans")
    assert table is not None
    assert table.domain == "credit"
    cols = {c.name: c for c in table.columns}
    assert cols["loan_id"].nullable is False
    assert cols["region_id"].relationship == "FK -> dim_region.region_id"
    # The accepted_values literal (incl. the secret) must never reach the model.
    assert SECRET not in dd.render_table(table)


def test_dbt_sources_nest_one_level(tmp_path):
    _write(
        tmp_path,
        "dictionaries/raw.yaml",
        """
sources:
  - name: stripe
    tables:
      - name: payment
        columns:
          - name: id
            data_type: int
""",
    )
    index = dd.load_index(tmp_path, "context")
    assert index.get("payment") is not None


def test_frictionless_shape(tmp_path):
    _write(
        tmp_path,
        "dictionaries/pkg.yaml",
        f"""
resources:
  - name: people
    schema:
      fields:
        - name: id
          type: integer
          description: row id
          example: {SECRET}
        - name: country
          type: string
      foreignKeys:
        - fields: [country]
          reference: {{resource: countries, fields: [code]}}
""",
    )
    index = dd.load_index(tmp_path, "context")
    (report,) = index.reports
    assert report.shape == "frictionless"
    table = index.get("people")
    assert table is not None
    cols = {c.name: c for c in table.columns}
    assert cols["id"].type == "integer" and cols["id"].description == "row id"
    assert cols["country"].relationship.startswith("FK -> countries")
    assert SECRET not in dd.render_table(table)  # 'example' dropped


def test_great_expectations_derives_names_and_drops_kwargs(tmp_path):
    _write(
        tmp_path,
        "dictionaries/orders.yaml",
        f"""
expectation_suite_name: orders.warning
expectations:
  - expectation_type: expect_column_values_to_be_in_set
    kwargs: {{column: status, value_set: ['a', 'b', '{SECRET}']}}
  - expectation_type: expect_column_values_to_be_of_type
    kwargs: {{column: amount, type_: float}}
""",
    )
    index = dd.load_index(tmp_path, "context")
    (report,) = index.reports
    assert report.shape == "great_expectations"
    table = index.get("orders.warning")
    assert table is not None
    cols = {c.name: c for c in table.columns}
    assert set(cols) == {"status", "amount"}
    assert cols["amount"].type == "float"
    assert SECRET not in dd.render_table(table)  # value_set dropped wholesale


def test_generic_map_keyed_tables_and_columns(tmp_path):
    _write(
        tmp_path,
        "dictionaries/app.yaml",
        f"""
tables:
  users:
    columns:
      id: {{type: integer, primary_key: true}}
      email: {{type: varchar, nullable: false, default: '{SECRET}', description: addr}}
""",
    )
    index = dd.load_index(tmp_path, "context")
    table = index.get("users")
    assert table is not None
    cols = {c.name: c for c in table.columns}
    assert cols["email"].nullable is False and cols["email"].description == "addr"
    assert SECRET not in dd.render_table(table)  # 'default' dropped


def test_declarative_mapping_overrides_keys(tmp_path):
    _write(
        tmp_path,
        "dictionaries/custom.yaml",
        """
schemas:
  - schema_name: credit
    entities:
      - entity: loan_book
        doc: the book
        attributes:
          - attr: loan_id
            sqlType: bigint
            note: pk
            ref: borrower.id
""",
    )
    _write(
        tmp_path,
        "dictionaries/custom.yaml.map.yaml",
        """
format: generic
group_key: schemas
tables_key: entities
name_key: entity
desc_key: doc
columns_key: attributes
column_name_key: attr
type_key: sqlType
column_desc_key: note
relationship_key: ref
""",
    )
    index = dd.load_index(tmp_path, "context")
    table = index.get("loan_book")
    assert table is not None and table.description == "the book"
    (col,) = table.columns
    assert col.name == "loan_id" and col.type == "bigint"
    assert col.description == "pk" and col.relationship == "FK -> borrower.id"


def test_oversized_file_is_rejected_not_parsed(tmp_path):
    _write(tmp_path, "dictionaries/big.yaml", "models: []\n" + "# pad\n" * 100)
    index = dd.load_index(tmp_path, "context", max_file_bytes=50)
    (report,) = index.reports
    assert report.shape == "error" and "cap" in report.error


def test_context_dir_escape_is_ignored(tmp_path):
    # A context_dir that resolves outside the workspace yields an empty index.
    index = dd.load_index(tmp_path, "../evil")
    assert index.is_empty() and index.reports == ()


def test_nested_value_under_allowed_key_is_not_leaked(tmp_path):
    # A list/dict placed under an ALLOWED key (description/type) must degrade to
    # empty, not be str()-ified into the slot (which would leak nested values).
    _write(
        tmp_path,
        "dictionaries/x.yaml",
        f"""
tables:
  - name: t
    description:
      summary: ok
      sample_rows: ['{SECRET}', 12345.67]
    columns:
      - name: c
        type: ['{SECRET}', 'other']
""",
    )
    index = dd.load_index(tmp_path, "context")
    table = index.get("t")
    assert table is not None
    assert table.description == ""  # nested dict dropped, not stringified
    assert table.columns[0].type == ""  # nested list dropped
    assert SECRET not in dd.render_table(table)


def test_oddly_typed_yaml_never_raises(tmp_path):
    # All of these are valid YAML but the "wrong" shape; load_index must not crash.
    _write(
        tmp_path,
        "dictionaries/a.yaml",
        "models:\n  - name: m\n    columns:\n"
        "      - name: c\n        data_type: int\n        tests: {not_null: {}}\n",
    )
    _write(
        tmp_path,
        "dictionaries/b.yaml",
        "expectation_suite_name: s\nexpectations:\n  - kwargs: [not, a, dict]\n",
    )
    _write(tmp_path, "dictionaries/c.yaml", "resources:\n  - name: r\n    schema: [oops]\n")
    index = dd.load_index(tmp_path, "context")  # must not raise
    assert len(index.reports) == 3
    # the dbt one still parses its column; the malformed ones degrade gracefully
    assert index.get("m") is not None


def test_frictionless_single_string_foreign_key(tmp_path):
    _write(
        tmp_path,
        "dictionaries/pkg.yaml",
        """
resources:
  - name: people
    schema:
      fields:
        - name: country
          type: string
      foreignKeys:
        - fields: country
          reference: {resource: countries, fields: code}
""",
    )
    index = dd.load_index(tmp_path, "context")
    table = index.get("people")
    assert table is not None
    (col,) = table.columns
    assert col.relationship == "FK -> countries.code"  # not iterated char-by-char


def test_generic_map_scalar_value_is_treated_as_type(tmp_path):
    _write(
        tmp_path, "dictionaries/app.yaml", "tables:\n  orders:\n    columns:\n      id: bigint\n"
    )
    table = dd.load_index(tmp_path, "context").get("orders")
    assert table is not None
    (col,) = table.columns
    assert col.name == "id" and col.type == "bigint"


def test_single_file_form_and_search_get_list(tmp_path):
    # The single-file form: context/datadictionary.yaml (not the per-domain folder).
    _write(
        tmp_path,
        "datadictionary.yaml",
        "tables:\n  - name: orders\n    columns:\n      - name: id\n        type: int\n",
    )
    index = dd.load_index(tmp_path, "context")
    assert index.get("orders") is not None
    assert [t.name for t in index.search("orders")] == ["orders"]
    assert "orders" in dd.render_listing(index)
