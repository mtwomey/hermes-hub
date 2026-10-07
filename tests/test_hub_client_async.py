"""Phase 1.4 (BEA-304): HubClient submit (return_immediately) + wait (GetTask);
ask = submit + wait, returning state=working at the client deadline."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from hermes_hub import hub_client as hub_client_mod
from hermes_hub.hub_client import DEFAULT_WAIT_SECONDS, HubClient, HubClientError
from hub_harness import LiveHub


def run(coro):
    return asyncio.run(coro)


def test_default_wait_is_270_seconds():
    assert DEFAULT_WAIT_SECONDS == 270.0


def test_submit_returns_task_id_without_waiting_for_the_spoke():
    with LiveHub(spokes=[{"name": "Olive", "reply": "late", "delay": 2.0}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        start = time.monotonic()
        sub = run(client.submit("Olive", "slow one"))
        assert time.monotonic() - start < 1.5
        assert sub["task_id"] and sub["context_id"]
        assert sub["state"] in ("submitted", "working")


def test_submit_sends_return_immediately_via_send_message(monkeypatch):
    seen = []
    real_post = httpx.AsyncClient.post

    async def spy(self, url, *a, **k):
        seen.append(k.get("json"))
        return await real_post(self, url, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "post", spy)
    with LiveHub(spokes=[{"name": "Olive"}]) as hub:
        run(HubClient(hub_url=hub.base_url).submit("Olive", "hi"))
    body = seen[0]
    assert body["method"] == "SendMessage"
    assert body["params"]["configuration"]["returnImmediately"] is True


def test_fast_task_returns_text_in_one_ask_call():
    with LiveHub(spokes=[{"name": "Olive", "reply": "forty-two"}]) as hub:
        result = run(HubClient(hub_url=hub.base_url).ask("Olive", "q"))
    assert result["state"] == "completed"
    assert result["text"] == "forty-two"
    assert result["task_id"]


def test_slow_task_ask_returns_working_with_task_id_not_an_error():
    with LiveHub(spokes=[{"name": "Olive", "reply": "eventually", "delay": 3.0}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "slow", wait_seconds=0.5))
        assert result["state"] == "working"
        assert result["task_id"] and result["text"] == ""
        assert result["elapsed_s"] >= 0
        final = run(client.wait(result["task_id"], 10))
    assert final["state"] == "completed"
    assert final["text"] == "eventually"


def test_wait_returns_working_at_deadline():
    with LiveHub(spokes=[{"name": "Olive", "delay": 3.0}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        sub = run(client.submit("Olive", "slow"))
        start = time.monotonic()
        out = run(client.wait(sub["task_id"], 0.6))
        took = time.monotonic() - start
    assert out["state"] == "working"
    assert 0.5 <= took < 2.5


def test_http_timeout_is_not_coupled_to_task_duration():
    """Each HTTP call is short; the client timeout no longer must exceed
    the task's duration (old 330 s coupling)."""
    assert hub_client_mod.DEFAULT_TIMEOUT_SECONDS <= 60


def test_failed_task_still_raises_with_hub_error_text():
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": "right"}]) as hub:
        with pytest.raises(HubClientError) as info:
            run(HubClient(hub_url=hub.base_url).ask("Olive", "x", credential="wrong"))
    assert "unauthorized" in str(info.value)
