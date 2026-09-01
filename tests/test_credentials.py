"""Tests for hermes_hub.credentials: portable spoke credential resolution
(Task 1.5). Resolution order: explicit arg > env var > macOS Keychain >
unset (dev mode)."""

from __future__ import annotations

import subprocess

from hermes_hub.credentials import resolve_spoke_credential


def test_explicit_argument_wins_over_everything(monkeypatch):
    monkeypatch.setenv("HERMES_HUB_SPOKE_CREDENTIAL", "from-env")
    result = resolve_spoke_credential("Olive", explicit="from-explicit")
    assert result == "from-explicit"


def test_env_var_used_when_no_explicit_argument(monkeypatch):
    monkeypatch.setenv("HERMES_HUB_SPOKE_CREDENTIAL", "from-env")
    result = resolve_spoke_credential("Olive")
    assert result == "from-env"


def test_keychain_used_when_no_explicit_or_env(monkeypatch):
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)

    def fake_run(args, **kwargs):
        assert args[0] == "security"
        assert "spoke:Olive:credential" in args
        return subprocess.CompletedProcess(args, 0, stdout="from-keychain\n", stderr="")

    monkeypatch.setattr("hermes_hub.credentials.shutil.which", lambda name: "/usr/bin/security")
    monkeypatch.setattr("hermes_hub.credentials.subprocess.run", fake_run)

    result = resolve_spoke_credential("Olive")
    assert result == "from-keychain"


def test_unset_falls_through_to_dev_mode_empty_string(monkeypatch):
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)
    monkeypatch.setattr("hermes_hub.credentials.shutil.which", lambda name: None)

    result = resolve_spoke_credential("Olive")
    assert result == ""


def test_missing_security_binary_falls_through_to_dev_mode(monkeypatch):
    """A missing `security` binary (e.g. Linux/RPi, V8 portability) is not
    an error; it falls through to dev mode, not an exception."""
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)
    monkeypatch.setattr("hermes_hub.credentials.shutil.which", lambda name: None)

    result = resolve_spoke_credential("Olive")
    assert result == ""


def test_keychain_miss_falls_through_to_dev_mode(monkeypatch):
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)
    monkeypatch.setattr("hermes_hub.credentials.shutil.which", lambda name: "/usr/bin/security")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 44, stdout="", stderr="not found")

    monkeypatch.setattr("hermes_hub.credentials.subprocess.run", fake_run)

    result = resolve_spoke_credential("Olive")
    assert result == ""
