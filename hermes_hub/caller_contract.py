"""Caller routing contract — the single source of truth.

These names are what an external (possibly non-Hermes) caller must use to
reach a spoke through the hub. The agent card advertises them; the hub
executor, client, CLI and plugin tools consume them. Nothing else in the
package may hard-code these literals (enforced by tests/test_caller_contract.py).

Credential *locations* here are the conventions on the hub's own Mac. No
secret value ever lives in this module or in anything rendered from it.
"""

from __future__ import annotations

#: Required A2A extension that carries this contract in the agent card.
EXTENSION_URI = "urn:hermes-hub:ext:spoke-routing:v1"

#: ``params.message.metadata`` keys.
META_TARGET_SPOKE = "targetSpoke"
META_SPOKE_CREDENTIAL = "spokeCredential"

#: macOS Keychain conventions (service shared by hub, spokes and callers).
KEYCHAIN_SERVICE = "hermes-hub"
HUB_TOKEN_ACCOUNT = "hub:external:token"
CALLER_CREDENTIAL_ACCOUNT_TEMPLATE = "caller:{spoke}:credential"

#: Environment fallbacks.
ENV_HUB_TOKEN = "HERMES_HUB_TOKEN"
ENV_CALLER_CREDENTIAL_PREFIX = "HERMES_HUB_PEER_CREDENTIAL_"
ENV_CALLER_CREDENTIAL_TEMPLATE = ENV_CALLER_CREDENTIAL_PREFIX + "{SPOKE_UPPER}"

#: A2A protocol version the hub's handler accepts. Callers MUST send it as
#: the ``A2A-Version`` header; without it the SDK assumes 0.3 and rejects the
#: call with JSON-RPC error -32009 (found by a cold-agent acceptance run).
A2A_PROTOCOL_VERSION = "1.0"
A2A_VERSION_HEADER = "A2A-Version"

#: JSON-RPC method callers should use to ask a spoke.
RECOMMENDED_METHOD = "SendStreamingMessage"


def caller_credential_account(spoke: str) -> str:
    return CALLER_CREDENTIAL_ACCOUNT_TEMPLATE.format(spoke=spoke)


def caller_credential_env(spoke: str) -> str:
    return ENV_CALLER_CREDENTIAL_PREFIX + spoke.upper().replace("-", "_")


def keychain_command(account: str) -> str:
    """Human/agent-runnable command that prints one Keychain secret."""
    return f"security find-generic-password -s {KEYCHAIN_SERVICE} -a '{account}' -w"
