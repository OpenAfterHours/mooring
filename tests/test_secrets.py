"""The best-effort secret scanner: high precision, value-free findings."""

from __future__ import annotations

from mooring.ai import secrets


def test_detects_connection_string_with_credentials():
    hits = secrets.scan("dsn: postgres://user:hunter2@db.internal:5432/prod")
    assert hits and hits[0].kind == "connection string with credentials"


def test_detects_keys_and_tokens():
    samples = {
        "private key block": "-----BEGIN RSA PRIVATE KEY-----",
        "AWS access key id": "AKIAIOSFODNN7EXAMPLE",
        "GitHub token": "ghp_" + "a" * 36,
        "JWT": "eyJabc12345.eyJpayload9.sigsigsigX",
    }
    for kind, text in samples.items():
        hits = secrets.scan(text)
        assert any(h.kind == kind for h in hits), kind


def test_detects_password_only_dsn():
    # No username, password present — must still be caught.
    assert secrets.has_secrets("cache: redis://:authpass@cache:6379")


def test_credential_free_url_is_not_flagged():
    # A plain URL without embedded credentials must not false-positive.
    assert secrets.scan("see https://example.com/docs/path for details") == []


def test_clean_schema_text_has_no_findings():
    # A normal data-dictionary description must not trip the scanner.
    text = "loan_status: current servicing status of the loan; FK -> dim_status.code"
    assert secrets.scan(text) == []
    assert secrets.has_secrets(text) is False


def test_finding_is_value_free():
    text = "line one\napi: postgres://u:p@h/db"
    (finding,) = secrets.scan(text)
    assert finding.line == 2
    assert "p@h" not in finding.kind and ":" not in finding.kind  # never the matched value
