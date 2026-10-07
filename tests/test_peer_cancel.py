"""Phase 3.4 (BEA-306): peer_cancel tool -> A2A CancelTask; in-flight guard cleared."""

from __future__ import annotations

import json

from hermes_hub.tools import inflight, peer_tools
from hub_harness import LiveHub


def call(handler, hub, **args):
    args.setdefault("hub_url", hub.base_url)
    return json.loads(handler(args))


def test_peer_cancel_registered_after_peer_wait_with_schema():
    names = [spec.name for spec in peer_tools.TOOL_SPECS]
    assert names.index("peer_cancel") == names.index("peer_wait") + 1
    assert peer_tools.PEER_CANCEL_SCHEMA["parameters"]["required"] == ["task_id"]


def test_peer_cancel_requires_task_id():
    out = json.loads(peer_tools.peer_cancel({"hub_url": "http://127.0.0.1:1"}))
    assert out["success"] is False and "task_id" in out["error"]


def test_peer_cancel_cancels_running_task_and_clears_guard():
    with LiveHub(spokes=[{"name": "Olive", "delay": 5.0}]) as hub:
        sub = peer_tools._run(peer_tools._client({"hub_url": hub.base_url}).submit("Olive", "slow"))
        inflight.set_inflight("sess-x", "Olive", task_id=sub["task_id"], request="slow")
        out = call(peer_tools.peer_cancel, hub, task_id=sub["task_id"], peer_name="Olive")
        status = call(peer_tools.peer_status, hub, task_id=sub["task_id"])
    assert out["success"] is True
    assert out["state"] == "canceled" and out["task_id"] == sub["task_id"]
    assert status["state"] == "canceled"
    assert inflight.get_inflight("sess-x", "Olive") is None


def test_peer_cancel_of_finished_task_is_an_error():
    with LiveHub(spokes=[{"name": "Olive", "reply": "done"}]) as hub:
        done = call(peer_tools.peer_ask, hub, peer_name="Olive", message="q")
        out = call(peer_tools.peer_cancel, hub, task_id=done["task_id"])
    assert out["success"] is False
