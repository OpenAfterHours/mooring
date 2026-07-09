"""The offline scan-target policy lifted out of cli.py into mooring.ai.scan."""

from __future__ import annotations

from types import SimpleNamespace

from mooring.ai import pii, scan, secrets

VALID_CARD = "4012888888881881"
VALID_IBAN = "GB82WEST12345698765432"
VALID_NHS = "9434765919"


def _index(*report_paths: str):
    """A minimal DictionaryIndex stand-in: only `.reports[*].path/.error` is read."""
    return SimpleNamespace(reports=[SimpleNamespace(path=p, error="") for p in report_paths])


def test_scan_pii_targets_walks_context_dict_and_notebooks(tmp_path):
    (tmp_path / "context" / "dictionaries").mkdir(parents=True)
    (tmp_path / "context" / "instructions.md").write_text(f"contact card {VALID_CARD}\n", "utf-8")
    (tmp_path / "context" / "dictionaries" / "credit.yaml").write_text(
        f"note: nhs {VALID_NHS}\n", "utf-8"
    )
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "notebooks" / "a.py").write_text(f"# iban {VALID_IBAN}\n", "utf-8")

    findings = scan.scan_pii_targets(
        tmp_path,
        "context",
        ("notebooks",),
        _index("context/dictionaries/credit.yaml"),
        None,
    )

    assert {k for _, _, k in findings} == {pii.CARD, pii.NHS, pii.IBAN}
    assert {p for p, _, _ in findings} == {
        "context/instructions.md",
        "context/dictionaries/credit.yaml",
        "notebooks/a.py",
    }
    # value-free: a finding is (rel, line, kind) — never the matched value
    for value in (VALID_CARD, VALID_IBAN, VALID_NHS):
        assert value not in repr(findings)


def test_scan_pii_targets_dedupes_notebook_already_in_a_folder(tmp_path):
    (tmp_path / "context").mkdir()
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "notebooks" / "a.py").write_text(f"card {VALID_CARD}\n", "utf-8")

    # a.py is reached by both the folder walk and notebook_rel — deduped to one hit.
    findings = scan.scan_pii_targets(
        tmp_path, "context", ("notebooks",), _index(), "notebooks/a.py"
    )
    assert [(p, k) for p, _, k in findings] == [("notebooks/a.py", pii.CARD)]


def test_scan_pii_targets_covers_loose_root_py(tmp_path):
    # A loose root .py syncs by default, so the pre-flight scan must cover it — otherwise a
    # root helper carrying PII would ship to the team unscanned.
    (tmp_path / "context").mkdir()
    (tmp_path / "constants.py").write_text(f"CARD = '{VALID_CARD}'\n", "utf-8")
    findings = scan.scan_pii_targets(tmp_path, "context", ("notebooks",), _index(), None)
    assert ("constants.py", pii.CARD) in [(p, k) for p, _, k in findings]


def test_scan_context_secrets_finds_secrets_in_context_only(tmp_path):
    token = "ghp_" + "a" * 36
    (tmp_path / "context" / "dictionaries").mkdir(parents=True)
    (tmp_path / "context" / "instructions.md").write_text(f"deploy token {token}\n", "utf-8")
    (tmp_path / "context" / "dictionaries" / "d.yaml").write_text("desc: clean\n", "utf-8")

    findings = scan.scan_context_secrets(tmp_path, "context", _index("context/dictionaries/d.yaml"))
    assert [(p, k) for p, _, k in findings] == [("context/instructions.md", "GitHub token")]
    assert token not in repr(findings)  # value-free
    # sanity: the kind label is what the underlying scanner emits
    assert findings[0][2] in {f.kind for f in secrets.scan(f"x {token}")}
