"""Locality: select the relevant dictionary slice; enrich the dataset schema."""

from __future__ import annotations

import types

from mooring.ai import locality
from mooring.ai.datadictionary import Column, DictionaryIndex, Table


def _index():
    fact = Table(
        name="fact_loans",
        domain="credit",
        columns=(
            Column("loan_id", "i64"),
            Column("region_id", "i64", relationship="FK -> dim_region.region_id"),
            Column("exposure_amt", "f64", description="exposure at default"),
        ),
    )
    dim = Table(
        name="dim_region",
        domain="credit",
        columns=(Column("region_id", "i64"), Column("region_name", "str")),
    )
    other = Table(name="unrelated", domain="hr", columns=(Column("emp_id", "i64"),))
    return DictionaryIndex(tables=(fact, dim, other))


def test_working_set_picks_by_dataset_and_notebook_and_expands_fk():
    tables, reasons, n_more = locality.working_set(
        _index(),
        dataset_columns={"loan_id", "region_id", "exposure_amt"},
        dataset_stem="fact_loans",
        notebook_source="df = pl.read_parquet('data/fact_loans.parquet')",
        notebook_rel="notebooks/credit/q2.py",
    )
    names = {t.name for t in tables}
    assert "fact_loans" in names
    assert "dim_region" in names  # pulled in via FK 1-hop expansion
    assert "unrelated" not in names
    assert "matches your dataset" in reasons["credit.fact_loans"]


def test_working_set_empty_index():
    assert locality.working_set(DictionaryIndex()) == ([], {}, 0)


def test_seed_text_mentions_pull_tools():
    tables, reasons, n_more = locality.working_set(
        _index(), dataset_stem="fact_loans", notebook_source="fact_loans"
    )
    text = locality.seed_text(tables, reasons, n_more)
    assert "mooring_search_dictionary" in text
    assert "fact_loans" in text


def test_enrich_dataset_schema_keeps_file_dtype_and_adds_annotation():
    schema = types.SimpleNamespace(
        columns=(("region_id", "Int64"), ("exposure_amt", "Float64"), ("misc", "Utf8")),
        n_rows=10,
    )
    out = locality.enrich_dataset_schema(schema, _index(), "data/loans.parquet")
    assert "region_id: Int64" in out  # the real file dtype is preserved verbatim
    assert "FK -> dim_region.region_id" in out  # dictionary annotation added
    assert "exposure at default" in out
    # an unmatched column gets no annotation appended (misc is the last line here)
    assert out.rstrip().endswith("- misc: Utf8")
