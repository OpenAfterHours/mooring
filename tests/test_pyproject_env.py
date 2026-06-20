"""Per-repo notebook-dependency handling (pyproject_env)."""

import pytest

from mooring import pyproject_env as pe


def test_scaffold_writes_minimal_pyproject_and_is_idempotent(tmp_path):
    assert pe.scaffold(tmp_path, name="acme/notebooks", lock=False) is True
    text = pe.pyproject_path(tmp_path).read_text(encoding="utf-8")
    assert "marimo>=0.23.9" in text
    assert "package = false" in text
    # Lean: the old baked-in analyst stack is never seeded.
    for pkg in ("polars", "altair", "plotly", "openpyxl", "fastexcel"):
        assert pkg not in text
    # Never overwrites an existing file.
    assert pe.scaffold(tmp_path, lock=False) is False


def test_scaffold_skips_lock_without_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: False)
    pe.scaffold(tmp_path, lock=True)
    assert pe.has_pyproject(tmp_path)
    assert not pe.lock_path(tmp_path).is_file()


class _FakeDist:
    def __init__(self, name, version="1.0", requires=None):
        self.name = name
        self.version = version
        self.requires = requires


def test_top_level_from_keeps_roots_drops_transitive_and_mooring():
    # Models `uvx --with polars mooring`: polars is a deliberate pick (nothing
    # depends on it); marimo/requests/narwhals are transitive; mooring is the tool.
    dists = [
        _FakeDist("mooring", requires=["marimo>=0.23.9", "requests"]),
        _FakeDist("marimo", requires=["narwhals"]),
        _FakeDist("narwhals"),
        _FakeDist("requests"),
        _FakeDist("polars"),
    ]
    assert pe._top_level_from(dists) == ["polars"]


def test_top_level_from_is_sorted_case_insensitively_and_ignores_nameless():
    dists = [
        _FakeDist("Seaborn"),
        _FakeDist("altair"),
        _FakeDist(None),  # a malformed dist with no Name is skipped, not a crash
    ]
    assert pe._top_level_from(dists) == ["altair", "Seaborn"]


def test_top_level_from_tolerates_missing_requires():
    # importlib.metadata returns None for a dist that declares no dependencies.
    assert pe._top_level_from([_FakeDist("polars", requires=None)]) == ["polars"]


def test_top_level_from_ignores_extra_gated_deps_and_normalizes_names():
    # marimo lists polars only under an extra -> polars stays a deliberate root;
    # requests requires charset_normalizer (underscore) -> the hyphenated dist must
    # still be recognised as transitive and dropped (PEP 503 name normalization).
    dists = [
        _FakeDist("mooring", requires=["marimo>=0.23.9", "requests"]),
        _FakeDist("marimo", requires=['polars; extra == "recommended"', "narwhals"]),
        _FakeDist("narwhals"),
        _FakeDist("polars"),
        _FakeDist("requests", requires=["charset_normalizer>=2"]),
        _FakeDist("charset-normalizer"),
    ]
    assert pe._top_level_from(dists) == ["polars"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sales", "sales-notebooks"),
        ("acme/notebooks", "acme-notebooks"),  # already ends in "notebooks"
        ("notebooks", "notebooks"),
        ("   ", "mooring-notebooks"),
        ("My Repo!", "my-repo-notebooks"),
    ],
)
def test_project_name(raw, expected):
    assert pe._project_name(raw) == expected


@pytest.mark.parametrize(
    ("req", "name"),
    [
        ("polars", "polars"),
        ("requests>=2.0", "requests"),
        ("foo[extra]>=1.0; python_version>'3.10'", "foo"),
        ("pkg @ https://example.com/pkg.whl", "pkg"),
        ("  scipy == 1.11  ", "scipy"),
        ("# comment", None),
        ("", None),
    ],
)
def test_bare_name(req, name):
    assert pe._bare_name(req) == name


def _write_pyproject(path, deps):
    body = ", ".join(f'"{d}"' for d in deps)
    pe.pyproject_path(path).write_text(
        f'[project]\nname = "x"\nversion = "0"\ndependencies = [{body}]\n',
        encoding="utf-8",
    )


def test_declared_deps_and_declares(tmp_path):
    _write_pyproject(tmp_path, ["marimo>=0.13", "Polars>=1"])
    assert pe.declared_deps(tmp_path) == ["marimo>=0.13", "Polars>=1"]
    assert pe.declares(tmp_path, "marimo")
    assert pe.declares(tmp_path, "polars")  # case-insensitive
    assert not pe.declares(tmp_path, "scipy")


def test_declared_deps_no_pyproject(tmp_path):
    assert pe.declared_deps(tmp_path) == []
    assert pe.missing_deps(tmp_path) == []


def test_missing_deps_and_status(tmp_path, monkeypatch):
    _write_pyproject(tmp_path, ["marimo", "scipy", "numpy"])
    installed = {"marimo"}
    monkeypatch.setattr(pe, "_is_installed", lambda n: n in installed)
    assert pe.missing_deps(tmp_path) == ["scipy", "numpy"]
    assert pe.dep_status(tmp_path) == [
        ("marimo", True),
        ("scipy", False),
        ("numpy", False),
    ]


def test_uv_wrappers_raise_without_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "uv_available", lambda: False)
    for call in (
        lambda: pe.add(tmp_path, ["polars"]),
        lambda: pe.remove(tmp_path, ["polars"]),
        lambda: pe.run_lock(tmp_path),
    ):
        with pytest.raises(pe.UvNotAvailable):
            call()


def test_export_requirements_is_top_level_minus_marimo(tmp_path):
    _write_pyproject(tmp_path, ["marimo>=0.13", "polars", "scipy>=1.11"])
    # Drives a frozen build's `uv add -r`; marimo is omitted (mooring bundles it).
    assert pe.export_requirements(tmp_path) == "polars\nscipy>=1.11\n"
