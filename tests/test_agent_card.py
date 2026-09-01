from hermes_hub.agent_card import agent_card_json, build_hub_agent_card
from hermes_hub.registry import SpokeRegistry


def test_card_with_no_spokes_has_no_skills():
    reg = SpokeRegistry()
    card = build_hub_agent_card(reg)
    assert list(card.skills) == []


def test_card_with_two_spokes_tags_each_skill_with_owning_spoke():
    reg = SpokeRegistry()
    reg.register(
        name="Olive",
        skills=[{"id": "general-reasoning", "name": "General reasoning", "description": "Reason about things."}],
    )
    reg.register(
        name="Pumpkin",
        skills=[{"id": "filesystem-search", "name": "Filesystem search", "description": "Search local files."}],
    )
    card = build_hub_agent_card(reg)
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
    card1 = build_hub_agent_card(reg)
    assert len(card1.skills) == 1

    reg.deregister("Olive")
    card2 = build_hub_agent_card(reg)
    assert len(card2.skills) == 0


def test_card_has_streaming_capability_and_bearer_security():
    reg = SpokeRegistry()
    card = build_hub_agent_card(reg)
    assert card.capabilities.streaming is True
    dumped = agent_card_json(card)
    assert "bearerAuth" in dumped["securitySchemes"]
