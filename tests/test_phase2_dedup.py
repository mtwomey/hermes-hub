"""Phase 2 (BEA-305): duplicate protection.

2.1 transport dedup on A2A ``messageId`` (D4): a repeat SendMessage carrying
a messageId the hub has already seen attaches to the existing task instead
of dispatching to the spoke again.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from hermes_hub.hub_client import HubClient
from hub_harness import LiveHub


def _send(base_url: str, message_id: str, text: str = "slow job") -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": text}],
                "messageId": message_id,
                "metadata": {"targetSpoke": "Slow"},
            },
            "configuration": {"returnImmediately": True},
        },
    }
    resp = httpx.post(f"{base_url}/a2a/v1", json=body, headers={"A2A-Version": "1.0"}, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    assert "error" not in payload, payload
    result = payload["result"]
    return result.get("task") or result


def _task_frames(conn) -> list:
    return [f for f in conn.received if f.get("type") == "task"]


def test_duplicate_message_id_attaches_to_existing_task_single_dispatch():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 0.6}]) as hub:
        first = _send(hub.base_url, "dup-msg-1")
        second = _send(hub.base_url, "dup-msg-1", text="slow job (rephrased retry)")
        assert first["id"] and second["id"] == first["id"]
        time.sleep(1.0)
        assert len(_task_frames(hub.connections["Slow"])) == 1


def test_distinct_message_ids_dispatch_separately():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 0.2}]) as hub:
        a = _send(hub.base_url, "msg-a")
        b = _send(hub.base_url, "msg-b")
        assert a["id"] != b["id"]
        time.sleep(0.6)
        assert len(_task_frames(hub.connections["Slow"])) == 2


def test_concurrent_duplicate_message_ids_single_dispatch():
    with LiveHub(spokes=[{"name": "Slow", "reply": "done", "delay": 0.6}]) as hub:

        async def go():
            return await asyncio.gather(
                *(asyncio.to_thread(_send, hub.base_url, "race-msg") for _ in range(3))
            )

        results = asyncio.run(go())
        assert len({r["id"] for r in results}) == 1
        time.sleep(1.0)
        assert len(_task_frames(hub.connections["Slow"])) == 1


def test_hub_client_message_id_is_fresh_per_request_and_reused_when_given():
    client = HubClient(hub_url="http://unused")
    kw = dict(context_id="", credential="", file_name="", file_bytes=None,
              file_mime_type="application/octet-stream")
    m1 = client._build_message("Slow", "x", **kw)
    m2 = client._build_message("Slow", "x", **kw)
    assert m1["messageId"] != m2["messageId"]
    m3 = client._build_message("Slow", "x", message_id="fixed-1", **kw)
    assert m3["messageId"] == "fixed-1"
