"""The marimo version floor: the loud runtime assert + the declared floors in sync."""

from __future__ import annotations

import pathlib
import tomllib

import marimo
import pytest

from mooring import marimo_rt
from mooring import pyproject_env as pe


def _set(monkeypatch, version: str) -> None:
    monkeypatch.setattr(marimo_rt, "_floor_checked", False)
    monkeypatch.setattr(marimo, "__version__", version)


def test_floor_passes_on_the_current_marimo(monkeypatch):
    _set(monkeypatch, marimo_rt.MARIMO_FLOOR_STR)
    marimo_rt._require_marimo_floor()  # no raise


@pytest.mark.parametrize("version", ["0.13.0", "0.23.8", "0.9.0"])
def test_floor_fails_loud_on_old_marimo(monkeypatch, version):
    _set(monkeypatch, version)
    with pytest.raises(marimo_rt.MarimoTooOld):
        marimo_rt._require_marimo_floor()


@pytest.mark.parametrize("version", ["", "garbage", "v1.2.3"])
def test_floor_fails_loud_on_unparseable_version(monkeypatch, version):
    # An unparseable version is treated as too old — never silently passed.
    _set(monkeypatch, version)
    with pytest.raises(marimo_rt.MarimoTooOld):
        marimo_rt._require_marimo_floor()


@pytest.mark.parametrize("version", ["0.23.9.dev1", "0.24.0", "1.0.0rc1"])
def test_floor_tolerates_suffixes_and_newer(monkeypatch, version):
    _set(monkeypatch, version)
    marimo_rt._require_marimo_floor()  # no raise


def test_a_failed_check_is_not_cached_as_passed(monkeypatch):
    # The once-flag must be set ONLY on success, so a later good path re-asserts.
    _set(monkeypatch, "0.13.0")
    with pytest.raises(marimo_rt.MarimoTooOld):
        marimo_rt._require_marimo_floor()
    assert marimo_rt._floor_checked is False


def test_declared_floors_match_the_asserted_floor():
    # marimo_rt.MARIMO_FLOOR_STR is the single source of truth; the scaffold seed
    # and mooring's own install dependency must declare the same minimum.
    assert marimo_rt.MARIMO_FLOOR_STR in pe.MARIMO_REQUIREMENT
    root = pathlib.Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    marimo_dep = next(d for d in data["project"]["dependencies"] if d.startswith("marimo"))
    assert marimo_rt.MARIMO_FLOOR_STR in marimo_dep
