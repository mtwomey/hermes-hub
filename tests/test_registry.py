from hermes_hub.registry import SpokeRegistry


def test_register_and_list_spoke():
    reg = SpokeRegistry()
    reg.register(name="Olive", skills=[{"id": "general-reasoning"}])
    assert [s.name for s in reg.list_connected()] == ["Olive"]


def test_deregister_removes_spoke():
    reg = SpokeRegistry()
    reg.register(name="Olive", skills=[])
    reg.deregister("Olive")
    assert reg.list_connected() == []


def test_list_connected_is_sorted_by_name():
    reg = SpokeRegistry()
    reg.register(name="Pumpkin", skills=[])
    reg.register(name="Olive", skills=[])
    assert [s.name for s in reg.list_connected()] == ["Olive", "Pumpkin"]


def test_get_returns_none_for_unknown_spoke():
    reg = SpokeRegistry()
    assert reg.get("Ghost") is None


def test_is_connected():
    reg = SpokeRegistry()
    assert reg.is_connected("Olive") is False
    reg.register(name="Olive", skills=[])
    assert reg.is_connected("Olive") is True


def test_register_preserves_skills_and_metadata():
    reg = SpokeRegistry()
    reg.register(
        name="Olive",
        skills=[{"id": "general-reasoning", "name": "General reasoning"}],
        metadata={"host": "192.0.2.229"},
    )
    olive = reg.get("Olive")
    assert olive.skills == [{"id": "general-reasoning", "name": "General reasoning"}]
    assert olive.metadata == {"host": "192.0.2.229"}
