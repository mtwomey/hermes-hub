"""Build the hub's aggregate A2A ``AgentCard`` (H5).

The hub's card advertises the union of all currently-connected spokes'
skills, each skill's ``metadata`` field tagged with the owning spoke's name
so a caller/routing layer can tell which spoke to address (H6: callers
address a spoke by name explicitly, so this tagging is informational/
routing-aid, not auto-selection).

Fresh code per H7/H13 — modeled on hermes-peer's ``agent_card.py`` shape but
not imported from it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
    StringList,
)
from a2a.server.request_handlers.response_helpers import agent_card_to_dict

from .registry import SpokeRegistry

BEARER_SCHEME_NAME = "bearerAuth"

DEFAULT_INPUT_MODES = ["text/plain", "application/json"]
DEFAULT_OUTPUT_MODES = ["text/plain", "application/json"]

SPOKE_NAME_METADATA_KEY = "spoke_name"


def _spoke_skill(spoke_name: str, raw_skill: Dict[str, Any]) -> AgentSkill:
    """Build one AgentSkill from a spoke-reported skill dict, tagged with
    the owning spoke's name.

    A2A's ``AgentSkill`` protobuf has no free-form metadata field of its own,
    so the spoke tag is namespaced into the skill's ``id`` (``<spoke>::<id>``)
    -- unique across spokes even if two spokes report the same skill id --
    and also embedded in the description so a human/LLM reading the card can
    see it without decoding the id convention.
    """
    skill_id = str(raw_skill.get("id") or "skill")
    name = str(raw_skill.get("name") or skill_id)
    description = str(raw_skill.get("description") or "")
    tags = list(raw_skill.get("tags") or [])
    examples = list(raw_skill.get("examples") or [])
    namespaced_id = f"{spoke_name}::{skill_id}"
    tagged_description = f"[spoke: {spoke_name}] {description}".strip()
    return AgentSkill(
        id=namespaced_id,
        name=name,
        description=tagged_description,
        tags=tags + [f"spoke:{spoke_name}"],
        examples=examples,
        input_modes=list(raw_skill.get("input_modes") or DEFAULT_INPUT_MODES),
        output_modes=list(raw_skill.get("output_modes") or DEFAULT_OUTPUT_MODES),
    )


def build_hub_agent_card(
    registry: SpokeRegistry,
    *,
    hub_name: str = "hermes-hub",
    base_url: str = "http://127.0.0.1:8770",
    rpc_path: str = "/a2a/v1",
    protocol_version: str = "1.0",
    binding: str = "JSONRPC",
) -> AgentCard:
    """Build the hub's AgentCard from currently-connected spokes (H5)."""
    connected = registry.list_connected()
    spoke_names = ", ".join(s.name for s in connected) if connected else "no spokes currently connected"

    card = AgentCard(
        name=hub_name,
        description=(
            f"{hub_name} routes requests to connected Hermes spokes over "
            f"outbound WebSocket connections. Currently connected: {spoke_names}. "
            "Address a specific spoke's skill by its namespaced id "
            "(\"<spoke>::<skill-id>\")."
        ),
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=list(DEFAULT_INPUT_MODES),
        default_output_modes=list(DEFAULT_OUTPUT_MODES),
    )
    card.supported_interfaces.append(
        AgentInterface(
            url=f"{base_url}{rpc_path}",
            protocol_binding=binding,
            protocol_version=protocol_version,
        )
    )
    card.security_schemes[BEARER_SCHEME_NAME].CopyFrom(
        SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="Bearer",
                description="Shared bearer token issued out of band to trusted callers.",
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
