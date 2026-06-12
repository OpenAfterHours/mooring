"""Truststore injection at CLI startup (cli._inject_truststore)."""

import truststore

from mooring import cli


def test_injects_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: called.append(True))
    cli._inject_truststore(env={})
    assert called == [True]


def test_env_escape_hatch_skips_injection(monkeypatch):
    called = []
    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: called.append(True))
    for value in ("0", "false", "No", " OFF "):
        cli._inject_truststore(env={"MOORING_TRUSTSTORE": value})
    assert not called


def test_injection_failure_warns_instead_of_raising(monkeypatch, capsys):
    def boom():
        raise RuntimeError("broken backend")

    monkeypatch.setattr(truststore, "inject_into_ssl", boom)
    cli._inject_truststore(env={})
    assert "could not enable the OS trust store" in capsys.readouterr().out
