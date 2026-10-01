from google.protobuf import json_format

from a2a.types import SendMessageRequest

from hermes_hub import caller_contract as cc
from hermes_hub.agent_card import agent_card_json, build_hub_agent_card
from hermes_hub.registry import SpokeRegistry


def _two_spoke_registry() -> SpokeRegistry:
    reg = SpokeRegistry()
    reg.register(
        name="Olive",
        skills=[{"id": "general-reasoning", "name": "General reasoning", "description": "Reason about things."}],
    )
    reg.register(
        name="Pumpkin",
        skills=[{"id": "filesystem-search", "name": "Filesystem search", "description": "Search local files."}],
    )
    return reg


def _routing_ext(dumped):
    exts = [e for e in dumped["capabilities"].get("extensions", []) if e["uri"] == cc.EXTENSION_URI]
    assert len(exts) == 1, dumped["capabilities"]
    return exts[0]


def test_card_with_no_spokes_has_no_skills():
    reg = SpokeRegistry()
    card = build_hub_agent_card(reg)
    assert list(card.skills) == []


def test_card_with_two_spokes_tags_each_skill_with_owning_spoke():
    card = build_hub_agent_card(_two_spoke_registry())
    dumped = agent_card_json(card)

    skills_by_id = {s["id"]: s for s in dumped["skills"]}
    assert "Olive::general-reasoning" in skills_by_id
    assert "Pumpkin::filesystem-search" in skills_by_id

    olive_skill = skills_by_id["Olive::general-reasoning"]
    pumpkin_skill = skills_by_id["Pumpkin::filesystem-search"]

    # PASS requires a human reading the JSON can tell which spoke owns which
    # skill: verify via tag and description, not just the namespaced id.
    assert "spoke:Olive" in olive_skill["tags"]
    assert "Olive" in olive_skill["description"]
    assert "spoke:Pumpkin" in pumpkin_skill["tags"]
    assert "Pumpkin" in pumpkin_skill["description"]


def test_card_reflects_registry_changes():
    reg = SpokeRegistry()
    reg.register(name="Olive", skills=[{"id": "general-reasoning"}])
    card1 = agent_card_json(build_hub_agent_card(reg))
    assert len(card1["skills"]) == 1
    assert _routing_ext(card1)["params"]["connectedSpokes"] == ["Olive"]

    reg.deregister("Olive")
    card2 = agent_card_json(build_hub_agent_card(reg))
    assert len(card2.get("skills", [])) == 0
    assert _routing_ext(card2)["params"]["connectedSpokes"] == []


def test_card_has_streaming_capability_and_bearer_security():
    reg = SpokeRegistry()
    card = build_hub_agent_card(reg)
    assert card.capabilities.streaming is True
    dumped = agent_card_json(card)
    assert "bearerAuth" in dumped["securitySchemes"]


# -- self-describing caller contract ----------------------------------------


def test_card_declares_required_spoke_routing_extension():
    dumped = agent_card_json(build_hub_agent_card(_two_spoke_registry()))
    ext = _routing_ext(dumped)
    assert ext["required"] is True
    params = ext["params"]
    assert sorted(params["connectedSpokes"]) == ["Olive", "Pumpkin"]
    meta = params["messageMetadata"]
    assert meta[cc.META_TARGET_SPOKE]["required"] is True
    assert meta[cc.META_SPOKE_CREDENTIAL]["required"] is False
    assert params["methods"]["ask"].startswith(cc.RECOMMENDED_METHOD)
    assert "GetTask" in params["methods"]["lookup"]
    # extension prose names the same keys the params do
    assert cc.META_TARGET_SPOKE in ext["description"]
    assert cc.META_SPOKE_CREDENTIAL in ext["description"]


def test_card_documents_keychain_locations_not_values():
    params = _routing_ext(agent_card_json(build_hub_agent_card(SpokeRegistry())))["params"]
    creds = params["credentials"]
    assert creds["hubToken"]["keychainService"] == cc.KEYCHAIN_SERVICE
    assert creds["hubToken"]["keychainAccount"] == cc.HUB_TOKEN_ACCOUNT
    assert creds["hubToken"]["command"] == cc.keychain_command(cc.HUB_TOKEN_ACCOUNT)
    assert creds["hubToken"]["envFallback"] == cc.ENV_HUB_TOKEN
    sc = creds["spokeCredential"]
    assert sc["keychainAccountTemplate"] == cc.CALLER_CREDENTIAL_ACCOUNT_TEMPLATE
    assert sc["envFallbackTemplate"] == cc.ENV_CALLER_CREDENTIAL_TEMPLATE


def test_card_description_states_targetSpoke_routing_and_drops_namespaced_hint():
    dumped = agent_card_json(build_hub_agent_card(_two_spoke_registry()))
    desc = dumped["description"]
    assert cc.META_TARGET_SPOKE in desc
    assert cc.META_SPOKE_CREDENTIAL in desc
    assert cc.EXTENSION_URI in desc
    assert "Address a specific spoke's skill by its namespaced id" not in desc
    assert "Olive" in desc and "Pumpkin" in desc


def test_bearer_scheme_description_names_keychain_account():
    dumped = agent_card_json(build_hub_agent_card(SpokeRegistry()))
    scheme = dumped["securitySchemes"]["bearerAuth"]["httpAuthSecurityScheme"]
    assert cc.HUB_TOKEN_ACCOUNT in scheme["description"]
    assert cc.KEYCHAIN_SERVICE in scheme["description"]


def test_skill_description_tells_caller_how_to_address_spoke():
    dumped = agent_card_json(build_hub_agent_card(_two_spoke_registry()))
    olive = next(s for s in dumped["skills"] if s["id"] == "Olive::general-reasoning")
    assert f'{cc.META_TARGET_SPOKE}="Olive"' in olive["description"]


def test_card_example_request_is_valid_send_message_request():
    params = _routing_ext(agent_card_json(build_hub_agent_card(_two_spoke_registry())))["params"]
    example = params["exampleRequest"]
    assert example["jsonrpc"] == "2.0"
    # JSON-RPC requires a string or integer id; Struct would turn 1 into 1.0.
    assert isinstance(example["id"], str)
    assert example["method"] == cc.RECOMMENDED_METHOD
    parsed = json_format.ParseDict(example["params"], SendMessageRequest())
    assert cc.META_TARGET_SPOKE in parsed.message.metadata.fields


def test_card_latency_reflects_configured_task_timeout():
    params = _routing_ext(
        agent_card_json(build_hub_agent_card(SpokeRegistry(), task_timeout_seconds=123))
    )["params"]
    assert "123" in params["latency"]
    assert "123.0" not in params["latency"]


def test_card_declares_required_headers_including_a2a_version():
    params = _routing_ext(agent_card_json(build_hub_agent_card(SpokeRegistry())))["params"]
    assert params["requiredHeaders"] == {
        "Authorization": "Bearer <hub token>",
        "A2A-Version": "1.0",
        "Content-Type": "application/json",
    }
    assert "A2A-Version: 1.0" in agent_card_json(build_hub_agent_card(SpokeRegistry()))["description"]


def test_card_documents_errors_and_local_url():
    params = _routing_ext(
        agent_card_json(build_hub_agent_card(SpokeRegistry(), base_url="http://10.0.0.5:8770"))
    )["params"]
    assert "401" in params["errors"] and "-32009" in params["errors"] and "error" in params["errors"]
    assert params["localRpcUrl"] == "http://127.0.0.1:8770/a2a/v1"


def test_spoke_credential_guidance_is_unambiguous():
    meta = _routing_ext(agent_card_json(build_hub_agent_card(SpokeRegistry())))["params"]["messageMetadata"]
    cred = meta[cc.META_SPOKE_CREDENTIAL]
    assert cred["required"] is False  # the hub does not require it
    assert "Keychain item exists" in cred["sendWhen"]


def test_card_rpc_url_matches_base_url():
    dumped = agent_card_json(build_hub_agent_card(SpokeRegistry(), base_url="http://10.0.0.5:8770"))
    assert dumped["supportedInterfaces"][0]["url"] == "http://10.0.0.5:8770/a2a/v1"
    params = _routing_ext(dumped)["params"]
    assert params["artifacts"].startswith("GET http://10.0.0.5:8770/a2a/artifacts/")
