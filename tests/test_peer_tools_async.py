"""Phase 1.5 (BEA-304): peer_ask working-state result, peer_wait, never re-ask."""

from __future__ import annotations

import json

from hermes_hub.tools import peer_tools
from hub_harness import LiveHub


def call(handler, hub, **args):
    args.setdefault("hub_url", hub.base_url)
    return json.loads(handler(args))


def test_peer_wait_is_registered_after_peer_status_with_schema():
    names = [spec.name for spec in peer_tools.TOOL_SPECS]
    assert names.index("peer_wait") == names.index("peer_status") + 1
    schema = peer_tools.PEER_WAIT_SCHEMA
    assert schema["parameters"]["required"] == ["task_id"]
    assert schema["parameters"]["properties"]["seconds"]["maximum"] == 270


def test_peer_ask_schema_tells_the_model_never_to_re_ask():
    assert "Do NOT send the same request again" in peer_tools.PEER_ASK_SCHEMA["description"]
    assert "peer_wait" in peer_tools.PEER_ASK_SCHEMA["description"]


def test_peer_ask_fast_reply_is_unchanged_plus_state():
    with LiveHub(spokes=[{"name": "Olive", "reply": "forty-two"}]) as hub:
        out = call(peer_tools.peer_ask, hub, peer_name="Olive", message="q")
    assert out["success"] is True and out["state"] == "completed"
    assert out["text"] == "forty-two" and "instruction" not in out


def test_peer_ask_deadline_returns_working_with_do_not_resend(monkeypatch):
    monkeypatch.setattr(peer_tools, "PEER_ASK_WAIT_SECONDS", 0.5)
    with LiveHub(spokes=[{"name": "Olive", "reply": "slow answer", "delay": 3.0}]) as hub:
        out = call(peer_tools.peer_ask, hub, peer_name="Olive", message="slow")
        assert out["success"] is True
        assert out["state"] == "working"
        assert out["task_id"] and out["context_id"]
        assert isinstance(out["elapsed_s"], int)
        assert "Olive" in out["instruction"]
        assert "Do NOT send this request again" in out["instruction"]
        assert f"peer_wait(task_id=\"{out['task_id']}\")" in out["instruction"]
        waited = call(peer_tools.peer_wait, hub, task_id=out["task_id"], seconds=10)
    assert waited["success"] is True
    assert waited["state"] == "completed" and waited["text"] == "slow answer"


def test_peer_wait_returns_working_again_with_instruction_at_deadline():
    with LiveHub(spokes=[{"name": "Olive", "delay": 3.0}]) as hub:
        sub = peer_tools._run(peer_tools._client({"hub_url": hub.base_url}).submit("Olive", "x"))
        out = call(peer_tools.peer_wait, hub, task_id=sub["task_id"], seconds=0.3)
    assert out["state"] == "working"
    assert "Do NOT send this request again" in out["instruction"]


def test_peer_wait_clamps_seconds_to_270():
    assert peer_tools.clamp_wait_seconds(9999) == 270.0
    assert peer_tools.clamp_wait_seconds(None) == 270.0
    assert peer_tools.clamp_wait_seconds(-5) == 0.0
    assert peer_tools.clamp_wait_seconds("12") == 12.0


def test_peer_wait_requires_task_id():
    out = json.loads(peer_tools.peer_wait({"hub_url": "http://127.0.0.1:1"}))
    assert out["success"] is False and "task_id" in out["error"]


def test_peer_wait_reports_failed_task_with_hermes_error():
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": "right"}]) as hub:
        client = peer_tools._client({"hub_url": hub.base_url})
        sub = peer_tools._run(client.submit("Olive", "x", credential="wrong"))
        out = call(peer_tools.peer_wait, hub, task_id=sub["task_id"], seconds=5)
    assert out["success"] is False
    assert "unauthorized" in out["error"]
    assert out["state"] == "failed" and out["hermes_error"] == "spoke_task_failed"
