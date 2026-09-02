"""M1 Gate 1: the distinctness assertion whose absence let every account on
Pumpkin and Olive collapse to one shared secret (W4b defect).

``assert_distinct_credential_topology`` must FAIL against a shared-secret
fixture (every account holds the same value -- exactly W4's undetected
state) and PASS against a fixture where intended pairs match each other but
differ from every other group.
"""

from __future__ import annotations

import pytest

from hermes_hub.credentials import (
    CredentialTopologyError,
    assert_distinct_credential_topology,
)

# Mirrors the plan's Task 1.2 table: three independent secrets.
#   hub_external : hub:external:token, must match Pumpkin <-> Olive
#   pumpkin_pair : spoke:Pumpkin:credential <-> every caller:Pumpkin:credential
#   olive_pair   : spoke:Olive:credential <-> every caller:Olive:credential
GROUPS = {
    "hub_external": [
        "hub:external:token (Pumpkin)",
        "hub:external:token (Olive)",
    ],
    "pumpkin_pair": [
        "spoke:Pumpkin:credential",
        "caller:Pumpkin:credential (Pumpkin)",
        "caller:Pumpkin:credential (Olive)",
    ],
    "olive_pair": [
        "spoke:Olive:credential",
        "caller:Olive:credential (Pumpkin)",
    ],
}


def _all_accounts() -> list[str]:
    return [account for accounts in GROUPS.values() for account in accounts]


def test_shared_secret_fixture_fails_distinctness():
    """The exact pre-M1 defect: every account resolves to ONE shared value.
    This must be rejected -- it is precisely what W4's completion matrix
    mistakenly read as proof of correct pairing."""
    shared_secret = "c446469249fdaac5-shared-fixture-value"
    values = {account: shared_secret for account in _all_accounts()}
    with pytest.raises(CredentialTopologyError):
        assert_distinct_credential_topology(values, GROUPS)


def test_distinct_secrets_pass_when_pairs_match_and_groups_differ():
    values: dict[str, str] = {}
    values.update({a: "hub-secret-AAAA" for a in GROUPS["hub_external"]})
    values.update({a: "pumpkin-secret-BBBB" for a in GROUPS["pumpkin_pair"]})
    values.update({a: "olive-secret-CCCC" for a in GROUPS["olive_pair"]})
    assert_distinct_credential_topology(values, GROUPS) is None


def test_internal_pair_mismatch_is_rejected():
    """An intended-matching pair (spoke <-> its own caller value) that
    silently drifted apart must also fail -- this is not just a
    cross-group check."""
    values: dict[str, str] = {}
    values.update({a: "hub-secret-AAAA" for a in GROUPS["hub_external"]})
    values.update({a: "pumpkin-secret-BBBB" for a in GROUPS["pumpkin_pair"]})
    values.update({a: "olive-secret-CCCC" for a in GROUPS["olive_pair"]})
    values["spoke:Olive:credential"] = "olive-secret-DIFFERENT"
    with pytest.raises(CredentialTopologyError):
        assert_distinct_credential_topology(values, GROUPS)


def test_missing_value_is_never_treated_as_matching():
    """An empty string (Keychain miss) must never be silently accepted as
    'matching' -- e3b0c44298fc1c14 is the hash of missing, not of equal."""
    values: dict[str, str] = {}
    values.update({a: "hub-secret-AAAA" for a in GROUPS["hub_external"]})
    values.update({a: "pumpkin-secret-BBBB" for a in GROUPS["pumpkin_pair"]})
    values.update({a: "olive-secret-CCCC" for a in GROUPS["olive_pair"]})
    values["caller:Olive:credential (Pumpkin)"] = ""
    with pytest.raises(CredentialTopologyError):
        assert_distinct_credential_topology(values, GROUPS)
