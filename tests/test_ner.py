"""The optional NER name detector (Phase 2 of the PII guard).

GLiNER (torch + transformers) is NOT a test/dev dependency, so these tests never
load a real model: they monkeypatch ``ner.load_model`` with a tiny fake that finds
known names by substring. That is enough to pin the parts mooring owns — the
value-free finding shape, line mapping, suppression, dedup, and the
backend-missing failure mode (which is exercised for real, since the extra is
absent in CI).
"""

from __future__ import annotations

import pytest

from mooring.ai import ner

SECRET_NAME = "Jon Harrison"  # a "name" that must never appear in a finding


class FakeModel:
    """Stand-in GLiNER: returns a value-bearing entity dict for each known name."""

    KNOWN = ("Jon Harrison", "Alice Smith")

    def __init__(self) -> None:
        self.calls: list[tuple[list, float]] = []

    def predict_entities(self, text, labels, threshold):
        self.calls.append((labels, threshold))
        out = []
        for name in self.KNOWN:
            idx = text.find(name)
            if idx >= 0:
                out.append(
                    {"text": name, "label": "person", "start": idx, "end": idx + len(name), "score": 0.95}
                )
        return out


@pytest.fixture
def fake_ner(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(ner, "load_model", lambda model=None: fake)
    return fake


# -- detection + value-free contract -------------------------------------------


def test_scan_names_maps_line_and_is_value_free(fake_ner):
    text = "intro line\nplease sum col_1 where the name is Jon Harrison now"
    findings = ner.scan_names(text)
    assert [(f.line, f.kind) for f in findings] == [(2, ner.NAME)]
    assert SECRET_NAME not in repr(findings)  # the matched name is dropped


def test_scan_names_across_multiple_lines(fake_ner):
    text = "Jon Harrison\nfiller\nask Alice Smith about it"
    lines = {f.line for f in ner.scan_names(text)}
    assert lines == {1, 3}


def test_scan_names_dedupes_per_line_and_kind(fake_ner):
    # Two names on one line collapse to a single (line, kind) finding.
    findings = ner.scan_names("pay Jon Harrison and Alice Smith today")
    assert [(f.line, f.kind) for f in findings] == [(1, ner.NAME)]


def test_scan_names_suppress_marker_blanks_the_line(fake_ner):
    assert ner.scan_names("name Jon Harrison  # mooring: pii-ok") == []


def test_scan_names_passes_labels_and_threshold(fake_ner):
    ner.scan_names("Jon Harrison", labels=("person", "name"), threshold=0.8)
    assert fake_ner.calls == [(["person", "name"], 0.8)]


# -- kind mapping --------------------------------------------------------------


def test_resolve_model_ref_defaults():
    # None -> the pinned safetensors default (id + revision + bf16 variant)
    ref = ner._resolve(None)
    assert ref.id == "gliner-community/gliner_small-v2.5"
    assert ref.revision == "f227d3cd637bd4e6757ae143935316d062393341"
    assert ref.variant == "bf16"
    # a bare string -> that id, latest commit, repo-default weights (no variant)
    bare = ner._resolve("some/model")
    assert bare == ner.ModelRef("some/model", "", "")
    # an explicit ref passes through untouched
    explicit = ner.ModelRef("x/y", "abc", "fp16")
    assert ner._resolve(explicit) is explicit


def test_allow_patterns_fetch_only_safetensors_variant():
    # a variant restricts the download to that safetensors file (never pytorch_model.bin)
    pats = ner._allow_patterns("bf16")
    assert "model.bf16.safetensors" in pats
    assert not any("pytorch_model.bin" in p for p in pats)
    # no variant -> no restriction (download the repo's default weights file)
    assert ner._allow_patterns("") is None


def test_kind_for_maps_person_and_org_labels():
    assert ner._kind_for("person") == ner.NAME
    assert ner._kind_for("Name") == ner.NAME
    assert ner._kind_for("first name") == ner.NAME
    # org-ish labels (incl. ones containing "name") surface as ORG, not person name
    assert ner._kind_for("organization") == ner.ORG
    assert ner._kind_for("company") == ner.ORG
    assert ner._kind_for("company name") == ner.ORG
    assert ner._kind_for("address") == "address"  # any other label, surfaced as-is


# -- backend-missing failure mode (real: the extra is not installed in CI) ------


def test_unavailable_without_extra_raises_loudly():
    if ner.available():
        pytest.skip("the 'pii' extra (gliner) is installed in this environment")
    assert ner.available() is False
    with pytest.raises(ner.NerUnavailable):
        ner.scan_names("contact Jon Harrison")


def test_cli_pii_model_reports_already_cached(tmp_path, monkeypatch, capsys):
    from mooring import paths
    from mooring.cli import main

    # Isolate the config against an empty user dir, so the developer's real
    # config.toml (which may select name_backend = "spacy") can't change which
    # backend branch `ai pii model` takes. Defaults -> the GLiNER backend.
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(ner, "available", lambda: True)
    monkeypatch.setattr(ner, "is_cached", lambda mid=None: True)
    assert main(["ai", "pii", "model"]) == 0
    assert "already downloaded" in capsys.readouterr().out
