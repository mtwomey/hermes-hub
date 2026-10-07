"""Card-only conformance: the drift guard (self-describing card plan, Task 5).

A "naive caller" that knows only the hub base URL and the hub token reads
the agent card and, using nothing but fields parsed from that JSON, builds a
request that reaches a spoke. If the card ever stops describing how routing
really works, these tests fail.

Deliberate constraint: apart from the in-process harness that starts a real
hub and attaches a fake spoke, this file must not import anything from
``hermes_hub`` — the caller under test has no knowledge of the package.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path

import httpx
import pytest

from hub_harness import FakeSpokeConnection, LiveHub

HUB_TOKEN = "TOK-sentinel-hub-7f3a"
OLIVE_CRED = "CRED-sentinel-olive-91c2"
ROUTING_URI_PATTERN = re.compile(r"spoke-routing")


# -- the naive caller -------------------------------------------------------


def _get_card(base_url: str, token: str) -> dict:
    resp = httpx.get(
        f"{base_url}/.well-known/agent-card.json",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _routing_extension(card: dict) -> dict:
    required = [e for e in card["capabilities"]["extensions"] if e.get("required")]
    assert len(required) == 1, "card must declare exactly one required extension"
    ext = required[0]
    assert ROUTING_URI_PATTERN.search(ext["uri"])
    return ext


def _headers_from_card(card: dict, token: str) -> dict:
    """Every HTTP header comes from the card, never from caller knowledge.

    Placeholders of the form ``<...>`` are filled with the token; this is the
    defect a cold-agent run found: the card omitted ``A2A-Version`` and this
    test masked it by hard-coding the header.
    """
    required = _routing_extension(card)["params"]["requiredHeaders"]
    return {k: re.sub(r"<[^>]+>", token, v) for k, v in required.items()}


def _build_request_from_card(card: dict, *, spoke: str, text: str, credential: str | None) -> tuple[str, dict]:
    ext = _routing_extension(card)
    params = ext["params"]
    rpc_url = card["supportedInterfaces"][0]["url"]
    assert rpc_url == params["rpcUrl"]

    meta_keys = params["messageMetadata"]
    target_key = next(k for k, v in meta_keys.items() if v["required"])
    cred_key = next(k for k, v in meta_keys.items() if not v["required"])

    body = copy.deepcopy(params["exampleRequest"])
    msg = body["params"]["message"]
    msg["messageId"] = f"naive-{time.time_ns()}"
    msg["parts"] = [{"text": text}]
    msg["metadata"] = {target_key: spoke}
    if credential is not None:
        msg["metadata"][cred_key] = credential
    return rpc_url, body


def _stream_terminal_state(rpc_url: str, body: dict, headers: dict) -> tuple[str, str]:
    """Follow the card's 'result' instructions over SSE."""
    state, text = "", ""
    with httpx.stream("POST", rpc_url, json=body, headers=headers, timeout=30) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:") :].strip())
            assert "error" not in payload, payload["error"]
            result = payload.get("result", {})
            task = result.get("task") or result.get("statusUpdate")
            if not task:
                continue
            status = task.get("status", {})
            state = status.get("state", state)
            msg = status.get("message")
            if msg:
                text = "".join(p.get("text", "") for p in msg.get("parts", []))
    return state, text


# -- tests ------------------------------------------------------------------


def test_card_alone_suffices_to_route_a_task_to_a_spoke():
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.add_spoke(
            name="Olive",
            skills=[{"id": "general-reasoning", "description": "Reason."}],
            reply=lambda frame: f"olive says: {frame.get('text', '')}",
            expected_credential=OLIVE_CRED,
        )
        card = _get_card(hub.base_url, HUB_TOKEN)
        assert "Olive" in _routing_extension(card)["params"]["connectedSpokes"]

        rpc_url, body = _build_request_from_card(card, spoke="Olive", text="ping", credential=OLIVE_CRED)
        assert rpc_url == f"{hub.base_url}/a2a/v1"

        state, text = _stream_terminal_state(rpc_url, body, _headers_from_card(card, HUB_TOKEN))
        assert state == "TASK_STATE_COMPLETED", text
        assert text == "olive says: ping"


def test_card_statement_that_spoke_rejects_missing_credential_is_true():
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.add_spoke(name="Olive", expected_credential=OLIVE_CRED)
        card = _get_card(hub.base_url, HUB_TOKEN)
        rpc_url, body = _build_request_from_card(card, spoke="Olive", text="ping", credential=None)
        state, text = _stream_terminal_state(rpc_url, body, _headers_from_card(card, HUB_TOKEN))
        assert state == "TASK_STATE_FAILED"
        assert "credential" in text


def test_card_statement_that_routing_requires_target_spoke_is_true():
    """Addressing by skill id alone (the old, wrong hint) fails."""
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.add_spoke(name="Olive", skills=[{"id": "general-reasoning"}])
        card = _get_card(hub.base_url, HUB_TOKEN)
        rpc_url, body = _build_request_from_card(card, spoke="Olive", text="ping", credential=None)
        body["params"]["message"]["metadata"] = {"skillId": "Olive::general-reasoning"}
        state, text = _stream_terminal_state(rpc_url, body, _headers_from_card(card, HUB_TOKEN))
        assert state == "TASK_STATE_FAILED"


class _SlowSpoke(FakeSpokeConnection):
    async def _respond(self, frame):
        await asyncio.sleep(1.5)
        await super()._respond(frame)


def test_send_message_blocks_until_terminal_as_card_states():
    """Pins the empirically observed behaviour the card's 'askSimple' text
    describes: non-streaming SendMessage waits for the slow spoke and returns
    the completed task in one body (observed 2026-10-01: 2.03s for a 2s spoke)."""
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.registry.register(name="Slow", skills=[])
        hub.router.register_connection("Slow", _SlowSpoke(router=hub.router, name="Slow", reply="done"))
        card = _get_card(hub.base_url, HUB_TOKEN)
        assert "blocks until the task is terminal" in _routing_extension(card)["params"]["methods"]["askSimple"]

        rpc_url, body = _build_request_from_card(card, spoke="Slow", text="hi", credential=None)
        body["method"] = "SendMessage"
        started = time.time()
        resp = httpx.post(
            rpc_url,
            json=body,
            headers=_headers_from_card(card, HUB_TOKEN),
            timeout=30,
        )
        elapsed = time.time() - started
        payload = resp.json()
        assert "error" not in payload, payload
        task = payload["result"]["task"]
        assert elapsed >= 1.4
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert "".join(p["text"] for p in task["status"]["message"]["parts"]) == "done"


def test_card_submit_then_poll_flow_returns_working_then_the_answer():
    """Phase 1.6 (D7): a naive caller follows ONLY the card's submit/poll
    contract: SendMessage with the card's configuration returns at once with
    a task id; GetTask polling later yields the completed answer."""
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.registry.register(name="Slow", skills=[])
        hub.router.register_connection("Slow", _SlowSpoke(router=hub.router, name="Slow", reply="done"))
        card = _get_card(hub.base_url, HUB_TOKEN)
        params = _routing_extension(card)["params"]
        submit = params["submit"]
        poll = params["poll"]
        rpc_url, body = _build_request_from_card(card, spoke="Slow", text="hi", credential=None)
        body["method"] = submit["method"]
        body["params"]["configuration"] = copy.deepcopy(submit["configuration"])
        headers = _headers_from_card(card, HUB_TOKEN)
        started = time.time()
        payload = httpx.post(rpc_url, json=body, headers=headers, timeout=30).json()
        assert time.time() - started < 1.0
        task = payload["result"]["task"]
        assert task["status"]["state"] in submit["initialStates"]
        deadline = time.time() + 10
        while True:
            lookup = {"jsonrpc": "2.0", "id": "2", "method": poll["method"], "params": {"id": task["id"]}}
            task = httpx.post(rpc_url, json=lookup, headers=headers, timeout=30).json()["result"]
            if task["status"]["state"] in poll["terminalStates"] or time.time() > deadline:
                break
            time.sleep(poll["intervalSeconds"])
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert "".join(p["text"] for p in task["status"]["message"]["parts"]) == "done"


def test_card_documents_task_ttl_and_hermes_error_codes():
    with LiveHub(external_token=HUB_TOKEN) as hub:
        params = _routing_extension(_get_card(hub.base_url, HUB_TOKEN))["params"]
        lifetime = params["taskLifetime"]
        assert lifetime["ttlSeconds"] == 20  # LiveHub's configured TTL (default 1800)
        assert "ttl_expired" in lifetime["onExpiry"]
        for code in ("ttl_expired", "spoke_disconnected", "spoke_unavailable", "spoke_task_failed"):
            assert code in params["hermesErrors"]
        assert "timeout" not in params["hermesErrors"]


def test_card_never_contains_secret_values(monkeypatch):
    monkeypatch.setenv("HERMES_HUB_TOKEN", HUB_TOKEN)
    monkeypatch.setenv("HERMES_HUB_PEER_CREDENTIAL_OLIVE", OLIVE_CRED)
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.add_spoke(name="Olive", skills=[{"id": "x"}], expected_credential=OLIVE_CRED)
        resp = httpx.get(
            f"{hub.base_url}/.well-known/agent-card.json",
            headers={"Authorization": f"Bearer {HUB_TOKEN}"},
            timeout=10,
        )
        assert resp.status_code == 200
        assert HUB_TOKEN not in resp.text
        assert OLIVE_CRED not in resp.text


def test_card_still_requires_the_hub_token():
    with LiveHub(external_token=HUB_TOKEN) as hub:
        resp = httpx.get(f"{hub.base_url}/.well-known/agent-card.json", timeout=10)
        assert resp.status_code == 401


def test_omitting_card_required_headers_fails_as_card_warns():
    """Each card-declared header is load-bearing: dropping A2A-Version yields
    the JSON-RPC error the card's 'errors' text describes."""
    with LiveHub(external_token=HUB_TOKEN) as hub:
        hub.add_spoke(name="Olive")
        card = _get_card(hub.base_url, HUB_TOKEN)
        params = _routing_extension(card)["params"]
        headers = _headers_from_card(card, HUB_TOKEN)
        assert "A2A-Version" in headers
        headers.pop("A2A-Version")
        rpc_url, body = _build_request_from_card(card, spoke="Olive", text="ping", credential=None)
        body["method"] = "SendMessage"
        resp = httpx.post(rpc_url, json=body, headers=headers, timeout=30)
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32009
        assert "-32009" in params["errors"]


def test_this_file_does_not_import_hermes_hub():
    src = Path(__file__).read_text()
    imports = [l for l in src.splitlines() if re.match(r"\s*(from|import)\s+hermes_hub", l)]
    assert imports == []
