"""Schema extraction must expose names + dtypes + counts — and never a value.

The secret-value assertions are the teeth of the "AI without sight of the data"
guarantee: we write real, sensitive cell values, then prove they appear nowhere
in the extracted schema or the text we'd send to the model.
"""

import polars as pl
import pytest
from openpyxl import Workbook

from mooring import schema

SECRET = "SECRET_VALUE_DO_NOT_LEAK"

DF = pl.DataFrame(
    {
        "region": ["EU", "US", "EU"],
        "amount": [100, 200, 300],
        "note": [SECRET, SECRET + "_2", SECRET + "_3"],
    }
)


def _write_xlsx(path):
    wb = Workbook()
    ws = wb.active
    ws.append(["region", "amount", "note"])
    ws.append(["EU", 100, SECRET])
    ws.append(["US", 200, SECRET + "_2"])
    wb.save(path)


@pytest.fixture
def parquet(tmp_path):
    p = tmp_path / "sales.parquet"
    DF.write_parquet(p)
    return p


@pytest.fixture
def csv(tmp_path):
    p = tmp_path / "sales.csv"
    DF.write_csv(p)
    return p


@pytest.fixture
def xlsx(tmp_path):
    p = tmp_path / "sales.xlsx"
    _write_xlsx(p)
    return p


def test_parquet_schema_names_dtypes_and_count(parquet):
    s = schema.extract_schema(parquet)
    names = [c[0] for c in s.columns]
    assert names == ["region", "amount", "note"]
    dtypes = dict(s.columns)
    assert dtypes["amount"] == "Int64"
    assert dtypes["region"] == "String"
    assert s.n_rows == 3


def test_csv_schema(csv):
    s = schema.extract_schema(csv)
    assert [c[0] for c in s.columns] == ["region", "amount", "note"]
    assert s.n_rows == 3


def test_xlsx_schema(xlsx):
    s = schema.extract_schema(xlsx)
    assert [c[0] for c in s.columns] == ["region", "amount", "note"]
    assert s.n_rows == 2  # two data rows, header excluded


@pytest.mark.parametrize("fixture", ["parquet", "csv", "xlsx"])
def test_no_data_value_ever_leaks(fixture, request):
    path = request.getfixturevalue(fixture)
    s = schema.extract_schema(path)
    blob = repr(s) + "\n" + schema.format_for_ai(s, source="data/sales" + path.suffix)
    assert SECRET not in blob
    assert "EU" not in blob and "US" not in blob  # even short string values
    assert "region" in blob and "note" in blob  # but names are present


def test_unsupported_extension_raises(tmp_path):
    p = tmp_path / "data.json"
    p.write_text("{}", "utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        schema.extract_schema(p)


def test_list_datasets_finds_supported_files_only(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "nested").mkdir()
    DF.write_parquet(tmp_path / "data" / "a.parquet")
    DF.write_csv(tmp_path / "data" / "nested" / "b.csv")
    (tmp_path / "data" / "notes.txt").write_text("ignore me", "utf-8")
    (tmp_path / "reports").mkdir()
    DF.write_parquet(tmp_path / "reports" / "c.parquet")

    found = schema.list_datasets(tmp_path, ("notebooks", "data", "reports"))
    assert found == ["data/a.parquet", "data/nested/b.csv", "reports/c.parquet"]


def test_format_for_ai_mentions_df_source_and_rows(parquet):
    s = schema.extract_schema(parquet)
    text = schema.format_for_ai(s, source="data/sales.parquet")
    assert "`df`" in text
    assert "data/sales.parquet" in text
    assert "3 rows" in text
    assert "region: String" in text
