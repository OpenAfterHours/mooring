"""The offline spaCy NER backend: value-free extraction + scan_names dispatch.

These run without spaCy installed — the model/predict are stubbed — so they pin
the value-free contract and the backend wiring in any environment.
"""

from __future__ import annotations

import importlib.util

import pytest

from mooring.ai import ner, ner_spacy

_HAS_SPACY = importlib.util.find_spec("spacy") is not None


class _Ent:
    def __init__(self, label: str, start: int) -> None:
        self.label_ = label
        self.start_char = start


class _Doc:
    def __init__(self, ents):
        self.ents = ents


class _Nlp:
    """A stub nlp: returns preset entities for a chunk (value-bearing in, value-free out)."""

    def __init__(self, by_chunk):
        self._by_chunk = by_chunk

    def __call__(self, chunk):
        return _Doc(self._by_chunk.get(chunk, []))


def test_predict_maps_labels_and_is_value_free():
    nlp = _Nlp({"Alice met Acme Corp": [_Ent("PERSON", 0), _Ent("ORG", 10)]})
    out = ner_spacy.predict(nlp, "Alice met Acme Corp", ("person", "organization"))
    assert out == [(ner.NAME, 0), (ner.ORG, 10)]
    # value-free: only (kind, start-offset) — never the matched text
    assert "Alice" not in repr(out) and "Acme" not in repr(out)


def test_predict_filters_to_wanted_labels():
    nlp = _Nlp({"x": [_Ent("PERSON", 0), _Ent("ORG", 5), _Ent("GPE", 9)]})
    assert ner_spacy.predict(nlp, "x", ("organization",)) == [(ner.ORG, 5)]
    # no labels -> default person + org (the GPE is dropped either way)
    assert ner_spacy.predict(nlp, "x", ()) == [(ner.NAME, 0), (ner.ORG, 5)]


def test_scan_names_spacy_dispatch_builds_value_free_findings(monkeypatch):
    # backend="spacy" loads via ner_spacy and runs the SHARED scan loop in ner.py
    # (char-offset -> line, dedupe, value-free Finding).
    monkeypatch.setattr(ner_spacy, "load", lambda model=None: object())
    monkeypatch.setattr(
        ner_spacy, "predict",
        lambda nlp, chunk, labels: [(ner.NAME, chunk.find("Bob"))] if "Bob" in chunk else [],
    )
    findings = ner.scan_names("line one\nhello Bob here", backend="spacy", labels=("person",))
    assert [(f.line, f.kind) for f in findings] == [(2, ner.NAME)]
    assert "Bob" not in repr(findings)


def test_scan_names_spacy_propagates_unavailable(monkeypatch):
    def boom(model=None):
        raise ner.NerUnavailable("no spaCy")

    monkeypatch.setattr(ner_spacy, "load", boom)
    with pytest.raises(ner.NerUnavailable):
        ner.scan_names("anything", backend="spacy")


@pytest.mark.skipif(_HAS_SPACY, reason="spaCy is installed in this environment")
def test_unavailable_without_the_extra():
    assert ner_spacy.available() is False
    assert ner.available("spacy") is False
    assert ner.is_ready(None, "spacy") is False
