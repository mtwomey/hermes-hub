"""The caller routing contract has exactly one source of truth.

The agent card advertises these names to non-Hermes callers; the executor,
client, CLI and plugin tools consume them. If any of them hard-coded its own
literal, the card could silently drift from real routing behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

from hermes_hub import caller_contract as cc

PKG = Path(__file__).resolve().parents[1] / "hermes_hub"

# literal -> why it must only live in caller_contract.py
GUARDED_LITERALS = [
    '"targetSpoke"',
    '"spokeCredential"',
    '"hub:external:token"',
    '"HERMES_HUB_PEER_CREDENTIAL_"',
    'f"caller:{',
]


def test_contract_values():
    assert cc.EXTENSION_URI == "urn:hermes-hub:ext:spoke-routing:v1"
    assert cc.META_TARGET_SPOKE == "targetSpoke"
    assert cc.META_SPOKE_CREDENTIAL == "spokeCredential"
    assert cc.KEYCHAIN_SERVICE == "hermes-hub"
    assert cc.HUB_TOKEN_ACCOUNT == "hub:external:token"
    assert cc.caller_credential_account("Olive") == "caller:Olive:credential"
    assert cc.caller_credential_env("Olive") == "HERMES_HUB_PEER_CREDENTIAL_OLIVE"
    assert cc.caller_credential_env("my-spoke") == "HERMES_HUB_PEER_CREDENTIAL_MY_SPOKE"


def test_no_module_hardcodes_contract_literals():
    offenders = []
    for path in PKG.rglob("*.py"):
        if path.name == "caller_contract.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.match(r"\s*(\"\"\"|''')", line):
                continue
            for lit in GUARDED_LITERALS:
                if lit in code:
                    offenders.append(f"{path.name}:{lineno}: {lit}")
    assert offenders == [], offenders
