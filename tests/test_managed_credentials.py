"""M2 managed-service credential posture tests.

Development helpers may still resolve an empty credential for portable unit
fixtures. A managed spoke is different: it must fail closed unless its V5a
credential is present in the Keychain resolution path. It never receives the
secret via plist/wrapper/argv.
"""

from __future__ import annotations

import pytest

from hermes_hub.credentials import CredentialUnavailable, require_spoke_credential


def test_managed_spoke_requires_a_resolved_credential(monkeypatch):
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)
    monkeypatch.setattr("hermes_hub.credentials._read_keychain", lambda name: "")

    with pytest.raises(CredentialUnavailable, match="Keychain credential"):
        require_spoke_credential("Pumpkin")


def test_managed_spoke_accepts_keychain_credential_without_exposing_value(monkeypatch):
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)
    monkeypatch.setattr("hermes_hub.credentials._read_keychain", lambda name: "keychain-secret")

    assert require_spoke_credential("Pumpkin") == "keychain-secret"


def test_managed_spoke_rejects_environment_credential(monkeypatch):
    """A launchd plist EnvironmentVariables block is world-readable; M2
    must not silently accept a secret supplied this way."""
    monkeypatch.setenv("HERMES_HUB_SPOKE_CREDENTIAL", "unsafe-env-secret")
    monkeypatch.setattr("hermes_hub.credentials._read_keychain", lambda name: "")

    with pytest.raises(CredentialUnavailable, match="environment"):
        require_spoke_credential("Pumpkin")
