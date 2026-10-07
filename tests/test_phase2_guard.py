"""Phase 2.2 in-flight guard + 2.3 context_id reuse (BEA-305)."""

from __future__ import annotations

import json
import time

import pytest

from hermes_hub.tools import inflight, peer_tools
from hub_harness import LiveHub


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(inflight, "store_path", lambda: tmp_path / "inflight.json")
    monkeypatch.setattr(peer_tools, "PEER_ASK_WAIT_SECONDS", 0.3)


def ask(hub, session="sess-A", **args):
    args.setdefault("hub_url", hub.base_url)
    args.setdefault("peer_name", "Slow")
    return json.loads(peer_tools.peer_ask(args, session_id=session, task_id=None))


def tasks(hub):
    return [f for f in hub.connections["Slow"].received if f.get("type") == "task"]


def test_caller_session_key_uses_session_id_else_process_fallback():
    assert inflight.caller_session_key({"session_id": "abc"}) == "abc"
    fb = inflight.caller_session_key({"session_id": None, "task_id": "t"})
    assert fb.startswith("proc-") and fb == inflight.caller_session_key({})


def test_second_ask_in_flight_returns_guard_and_does_not_send():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 2.0}]) as hub:
        first = ask(hub, message="summarise the logs")
        assert first["state"] == "working"
        second = ask(hub, message="please summarize those logs")
        assert second["state"] == "already_in_progress"
        assert second["task_id"] == first["task_id"]
        assert second["original_request"] == "summarise the logs"
        assert "peer_wait" in second["instruction"] and "new_request=true" in second["instruction"]
        assert len(tasks(hub)) == 1


def test_new_request_true_bypasses_guard():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 2.0}]) as hub:
        ask(hub, message="job one")
        forced = ask(hub, message="job two", new_request=True)
        assert forced["state"] == "working"
        assert len(tasks(hub)) == 2


def test_other_session_is_not_guarded():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 2.0}]) as hub:
        ask(hub, message="job")
        other = ask(hub, session="sess-B", message="job")
        assert other["state"] == "working"
        assert len(tasks(hub)) == 2


def test_after_completion_asks_normally():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 0.5}]) as hub:
        first = ask(hub, message="job")
        assert first["state"] == "working"
        time.sleep(0.8)
        again = ask(hub, message="next job")
        assert again["state"] in ("working", "completed")
        assert len(tasks(hub)) == 2


def test_guard_survives_process_restart_via_store():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 2.0}]) as hub:
        first = ask(hub, message="job")
        data = json.loads(inflight.store_path().read_text())
        assert any(e["task_id"] == first["task_id"] for e in data["inflight"].values())


def test_follow_up_reuses_last_context_id_for_session_and_peer():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done"}]) as hub:
        monkey_wait = 5.0
        peer_tools.PEER_ASK_WAIT_SECONDS = monkey_wait
        first = ask(hub, message="hello")
        second = ask(hub, message="follow up")
        third = ask(hub, session="sess-B", message="other session")
        assert first["context_id"] and second["context_id"] == first["context_id"]
        assert third["context_id"] != first["context_id"]
        frames = tasks(hub)
        assert frames[1]["context_id"] == frames[0]["context_id"]


def test_peer_ask_schema_has_new_request_flag():
    props = peer_tools.PEER_ASK_SCHEMA["parameters"]["properties"]
    assert props["new_request"]["type"] == "boolean"
