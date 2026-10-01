"""Build the hub's aggregate, self-describing A2A ``AgentCard`` (H5).

The card advertises the union of all currently-connected spokes' skills and
— so that a caller who has only the hub token and the card URL can proceed
with no other documentation — the complete caller contract:

* the card ``description`` states, in prose, how to ask a spoke;
* a **required** A2A ``AgentExtension`` (``caller_contract.EXTENSION_URI``)
  carries the same contract as structured ``params``: routing metadata keys,
  where credentials live on this Mac (Keychain locations, never values),
  methods, response handling, artifacts, latency, and a complete example;
* the bearer security scheme's description names the token's Keychain
  location;
* every skill's description says how to address its owning spoke.

All names come from :mod:`hermes_hub.caller_contract`, the same constants the
executor routes on, so the card cannot drift from real behaviour
(``tests/test_card_conformance.py`` proves it end to end).

No secret value is ever placed in the card.

Fresh code per H7/H13 — modeled on hermes-peer's ``agent_card.py`` shape but
not imported from it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
    StringList,
)
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from google.protobuf.struct_pb2 import Struct

from . import caller_contract as cc
from .artifacts import ARTIFACT_DOWNLOAD_PATH
from .registry import SpokeRegistry

BEARER_SCHEME_NAME = "bearerAuth"

DEFAULT_INPUT_MODES = ["text/plain", "application/json"]
DEFAULT_OUTPUT_MODES = ["text/plain", "application/json"]

SPOKE_NAME_METADATA_KEY = "spoke_name"

_HUB_TOKEN_CMD = cc.keychain_command(cc.HUB_TOKEN_ACCOUNT)
_SPOKE_CRED_CMD = cc.keychain_command(cc.CALLER_CREDENTIAL_ACCOUNT_TEMPLATE)


def _fmt_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _spoke_skill(spoke_name: str, raw_skill: Dict[str, Any]) -> AgentSkill:
    """Build one AgentSkill from a spoke-reported skill dict, tagged with
    the owning spoke's name.

    A2A's ``AgentSkill`` has no free-form metadata field, so the spoke is
    namespaced into the ``id`` (``<spoke>::<id>``, unique across spokes) and
    stated in the description together with how to address it. The id is
    informational only: routing is by ``metadata.targetSpoke``.
    """
    skill_id = str(raw_skill.get("id") or "skill")
    name = str(raw_skill.get("name") or skill_id)
    description = str(raw_skill.get("description") or "")
    tags = list(raw_skill.get("tags") or [])
    examples = list(raw_skill.get("examples") or [])
    namespaced_id = f"{spoke_name}::{skill_id}"
    tagged_description = (
        f'[spoke: {spoke_name} — address with metadata {cc.META_TARGET_SPOKE}="{spoke_name}"] '
        f"{description}"
    ).strip()
    return AgentSkill(
        id=namespaced_id,
        name=name,
        description=tagged_description,
        tags=tags + [f"spoke:{spoke_name}"],
        examples=examples,
        input_modes=list(raw_skill.get("input_modes") or DEFAULT_INPUT_MODES),
        output_modes=list(raw_skill.get("output_modes") or DEFAULT_OUTPUT_MODES),
    )


def caller_contract_params(
    *,
    connected_spokes: List[str],
    base_url: str,
    rpc_url: str,
    task_timeout_seconds: float,
) -> Dict[str, Any]:
    """The structured caller contract carried in the routing extension."""
    timeout = _fmt_seconds(task_timeout_seconds)
    example_spoke = connected_spokes[0] if connected_spokes else "<spoke name>"
    return {
        "connectedSpokes": list(connected_spokes),
        "rpcUrl": rpc_url,
        "messageMetadata": {
            cc.META_TARGET_SPOKE: {
                "required": True,
                "meaning": (
                    "Exact name of the spoke to ask (one of connectedSpokes). "
                    "This is the ONLY routing key; skill ids are informational."
                ),
            },
            cc.META_SPOKE_CREDENTIAL: {
                "required": False,
                "meaning": (
                    "Opaque per-spoke caller secret. The hub relays it without "
                    "checking; the spoke fails the task if it does not match. "
                    "Spokes with a configured secret require it, so send it "
                    "whenever the Keychain item exists."
                ),
            },
        },
        "credentials": {
            "scope": (
                "Conventions on the hub's own Mac (macOS login Keychain of the "
                "hub's user). Read values at call time; never print, log, store "
                "or echo them."
            ),
            "hubToken": {
                "use": "HTTP header 'Authorization: Bearer <hub token>' on every request",
                "keychainService": cc.KEYCHAIN_SERVICE,
                "keychainAccount": cc.HUB_TOKEN_ACCOUNT,
                "command": _HUB_TOKEN_CMD,
                "envFallback": cc.ENV_HUB_TOKEN,
            },
            cc.META_SPOKE_CREDENTIAL: {
                "use": f"params.message.metadata.{cc.META_SPOKE_CREDENTIAL}",
                "keychainService": cc.KEYCHAIN_SERVICE,
                "keychainAccountTemplate": cc.CALLER_CREDENTIAL_ACCOUNT_TEMPLATE,
                "command": _SPOKE_CRED_CMD,
                "envFallbackTemplate": cc.ENV_CALLER_CREDENTIAL_TEMPLATE,
            },
        },
        "methods": {
            "ask": (
                f"{cc.RECOMMENDED_METHOD} — POST JSON-RPC 2.0 to rpcUrl; the response "
                "is Server-Sent Events, one 'data: {json}' line per update."
            ),
            "askSimple": (
                "SendMessage — same params; blocks until the task is terminal and "
                "returns one JSON body with result.task. Set your HTTP client "
                f"timeout above the hub task timeout ({timeout}s)."
            ),
            "result": (
                "Find result.task or result.statusUpdate; when status.state is "
                "TASK_STATE_COMPLETED the answer is the concatenation of "
                "status.message.parts[].text. TASK_STATE_FAILED: the same text is "
                "the error (e.g. missing targetSpoke, spoke offline, credential "
                "rejected). TASK_STATE_WORKING updates are progress only."
            ),
            "followUp": (
                "Keep the returned contextId and send it as params.message.contextId "
                "to continue the same conversation with that spoke."
            ),
            "lookup": 'GetTask with params {"id": "<task id>"} returns a task\'s current state.',
        },
        "artifacts": (
            f"GET {base_url}{ARTIFACT_DOWNLOAD_PATH}/{{taskId}}/{{artifactId}} with "
            "the same bearer token. Artifact ids, names, sha256 and sizes arrive "
            "as artifact events in the response."
        ),
        "files": (
            "To send a file, add a part {\"raw\": <base64>, \"filename\": ..., "
            "\"mediaType\": ...} alongside the text part."
        ),
        "latency": (
            "A spoke runs a full agent turn: expect roughly 30 seconds to several "
            f"minutes. The hub fails a task after {timeout} seconds."
        ),
        # String id on purpose: protobuf Struct stores numbers as doubles, so
        # an integer id would serialize as 1.0, which JSON-RPC servers reject.
        "exampleRequest": {
            "jsonrpc": "2.0",
            "id": "1",
            "method": cc.RECOMMENDED_METHOD,
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "messageId": "<unique id, e.g. a UUID>",
                    "parts": [{"text": "<your question>"}],
                    "metadata": {
                        cc.META_TARGET_SPOKE: example_spoke,
                        cc.META_SPOKE_CREDENTIAL: "<value from the spokeCredential command>",
                    },
                }
            },
        },
    }


def _extension_description(rpc_url: str) -> str:
    return (
        "REQUIRED. hermes-hub routes by message metadata, not by skill id. "
        f"POST JSON-RPC 2.0 ({cc.RECOMMENDED_METHOD} or SendMessage) to {rpc_url} with "
        "'Authorization: Bearer <hub token>'. Set "
        f"params.message.metadata.{cc.META_TARGET_SPOKE} to the spoke's exact name and "
        f"params.message.metadata.{cc.META_SPOKE_CREDENTIAL} to that spoke's caller "
        f"credential. On this Mac the hub token is `{_HUB_TOKEN_CMD}` and a spoke's "
        f"credential is `{_SPOKE_CRED_CMD}` (substitute the spoke name). "
        "params carries the full machine-readable contract and an exampleRequest."
    )


def build_hub_agent_card(
    registry: SpokeRegistry,
    *,
    hub_name: str = "hermes-hub",
    base_url: str = "http://127.0.0.1:8770",
    rpc_path: str = "/a2a/v1",
    protocol_version: str = "1.0",
    binding: str = "JSONRPC",
    task_timeout_seconds: float = 300.0,
) -> AgentCard:
    """Build the hub's AgentCard from currently-connected spokes (H5)."""
    connected = registry.list_connected()
    names = [s.name for s in connected]
    spoke_names = ", ".join(names) if names else "no spokes currently connected"
    rpc_url = f"{base_url}{rpc_path}"

    description = (
        f"{hub_name} relays requests to named Hermes agents (\"spokes\") on other "
        f"machines. Connected now: {spoke_names}.\n"
        f"HOW TO ASK A SPOKE: POST a JSON-RPC 2.0 request, method "
        f"{cc.RECOMMENDED_METHOD} (streaming SSE) or SendMessage (blocking), to "
        f"{rpc_url} with header 'Authorization: Bearer <hub token>'. Put the target "
        f"spoke's exact name in params.message.metadata.{cc.META_TARGET_SPOKE} and "
        f"that spoke's caller credential in "
        f"params.message.metadata.{cc.META_SPOKE_CREDENTIAL}. Routing is by "
        f"{cc.META_TARGET_SPOKE} only; skill ids (\"<spoke>::<skill>\") are "
        "informational. The answer is in status.message.parts[].text once "
        "status.state is TASK_STATE_COMPLETED.\n"
        f"CREDENTIALS (this Mac): hub token `{_HUB_TOKEN_CMD}`; spoke credential "
        f"`{_SPOKE_CRED_CMD}`. Never print or store them.\n"
        f"Full contract, response handling and an example request: "
        f"capabilities.extensions[uri={cc.EXTENSION_URI}]."
    )

    card = AgentCard(
        name=hub_name,
        description=description,
        version="0.2.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=list(DEFAULT_INPUT_MODES),
        default_output_modes=list(DEFAULT_OUTPUT_MODES),
    )
    card.supported_interfaces.append(
        AgentInterface(
            url=rpc_url,
            protocol_binding=binding,
            protocol_version=protocol_version,
        )
    )

    params = Struct()
    params.update(
        caller_contract_params(
            connected_spokes=names,
            base_url=base_url,
            rpc_url=rpc_url,
            task_timeout_seconds=task_timeout_seconds,
        )
    )
    card.capabilities.extensions.append(
        AgentExtension(
            uri=cc.EXTENSION_URI,
            required=True,
            description=_extension_description(rpc_url),
            params=params,
        )
    )

    card.security_schemes[BEARER_SCHEME_NAME].CopyFrom(
        SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="Bearer",
                description=(
                    "Hub token, required on every request including this card. On "
                    f"the hub's Mac: macOS Keychain service '{cc.KEYCHAIN_SERVICE}', "
                    f"account '{cc.HUB_TOKEN_ACCOUNT}' (`{_HUB_TOKEN_CMD}`); env "
                    f"fallback {cc.ENV_HUB_TOKEN}."
                ),
            )
        )
    )
    requirement = card.security_requirements.add()
    requirement.schemes[BEARER_SCHEME_NAME].CopyFrom(StringList())

    skills: List[AgentSkill] = []
    for spoke in connected:
        for raw_skill in spoke.skills:
            skills.append(_spoke_skill(spoke.name, raw_skill))
    card.skills.extend(skills)
    return card


def agent_card_json(card: AgentCard) -> Dict[str, Any]:
    """Serialize a card to its A2A wire (camelCase) representation."""
    return agent_card_to_dict(card)
