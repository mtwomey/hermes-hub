"""M1 (Option 1, W4b): decouple the hub's shared spoke-registration ("join")
token from each spoke's distinct V5a command credential.

Before this fix, ``real_spoke.py`` sent its own per-spoke V5a credential
(``spoke:<name>:credential``) as the WebSocket registration token compared
by the hub against the single ``hub:spoke:token`` value. Once M1 makes
per-spoke credentials genuinely distinct, that coupling means only ONE
spoke could ever match the hub's single registration secret -- the other
would be locked out at the WebSocket handshake, before V5a even applies.

``require_join_credential`` resolves a separate, shared secret (Keychain
account ``hub:spoke:token`` -- unrelated to any ``spoke:<name>:credential``)
that every managed spoke presents to answer only V5's question (a): "may
this spoke join the hub at all". It fails closed, matching
``require_spoke_credential``'s managed-service posture: no env-var
override (world-readable launchd plists), Keychain only.
"""

from __future__ import annotations

import pytest

from hermes_hub.credentials import CredentialUnavailable, require_join_credential


def test_managed_spoke_requires_a_resolved_join_credential(monkeypatch):
    monkeypatch.delenv("HERMES_HUB_SPOKE_CREDENTIAL", raising=False)
    monkeypatch.setattr(
        "hermes_hub.credentials._read_keychain_account", lambda account: ""
    )

    with pytest.raises(CredentialUnavailable, match="join"):
        require_join_credential()


def test_managed_spoke_reads_join_credential_from_hub_spoke_token_account(monkeypatch):
    seen = {}

    def fake_read(account):
        seen["account"] = account
        return "join-secret" if account == "hub:spoke:token" else ""

    monkeypatch.setattr("hermes_hub.credentials._read_keychain_account", fake_read)

    assert require_join_credential() == "join-secret"
    assert seen["account"] == "hub:spoke:token"


def test_join_credential_is_independent_of_per_spoke_command_credential(monkeypatch):
    """The whole point of the fix: a distinct spoke:<name>:credential must
    not leak into, or be conflated with, the join token resolution."""

    def fake_read(account):
        if account == "hub:spoke:token":
            return "join-secret"
        if account == "spoke:Olive:credential":
            return "olive-command-secret-should-not-be-used-here"
        return ""

    monkeypatch.setattr("hermes_hub.credentials._read_keychain_account", fake_read)

    assert require_join_credential() == "join-secret"
