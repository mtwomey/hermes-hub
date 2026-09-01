"""Unit tests for the hub's ASGI app: agent card routes, health, and the
spoke WebSocket registration handshake -- via Starlette's in-process
TestClient (no real network socket needed for these)."""

from __future__ import annotations

import json

from starlette.testclient import TestClient

from hermes_hub.hub_server import build_hub_app


def test_agent_card_route_reflects_registry_with_no_spokes():
    app = build_hub_app()
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "hermes-hub"
    assert card.get("skills", []) == []


def test_health_route_reports_no_spokes_initially():
    app = build_hub_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "connected_spokes": []}


def test_spoke_websocket_registers_and_appears_in_registry_and_card():
    app = build_hub_app()
    client = TestClient(app)
    with client.websocket_connect("/hub/v1/spoke") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "register",
                    "name": "Olive",
                    "token": "",
                    "skills": [{"id": "general-reasoning", "name": "General reasoning"}],
                }
            )
        )
        # give the server a moment to process; TestClient is synchronous
        # over the ASGI app so the registration completes before we can
        # issue the next HTTP call on this same client.
        resp = client.get("/health")
        assert resp.json() == {"status": "ok", "connected_spokes": ["Olive"]}

        card_resp = client.get("/.well-known/agent-card.json")
        card = card_resp.json()
        skill_ids = [s["id"] for s in card["skills"]]
        assert "Olive::general-reasoning" in skill_ids

    # After the `with` block closes the websocket, the spoke should
    # deregister.
    resp_after = client.get("/health")
    assert resp_after.json() == {"status": "ok", "connected_spokes": []}


def test_spoke_websocket_rejects_bad_token():
    app = build_hub_app(expected_spoke_token="correct-token")
    client = TestClient(app)
    with client.websocket_connect("/hub/v1/spoke") as ws:
        ws.send_text(
            json.dumps({"type": "register", "name": "Olive", "token": "wrong-token", "skills": []})
        )
        # Server should close the connection; receiving text should raise.
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()

    resp = client.get("/health")
    assert resp.json() == {"status": "ok", "connected_spokes": []}


def test_spoke_websocket_accepts_correct_token():
    app = build_hub_app(expected_spoke_token="correct-token")
    client = TestClient(app)
    with client.websocket_connect("/hub/v1/spoke") as ws:
        ws.send_text(
            json.dumps({"type": "register", "name": "Olive", "token": "correct-token", "skills": []})
        )
        resp = client.get("/health")
        assert resp.json() == {"status": "ok", "connected_spokes": ["Olive"]}


def test_spoke_websocket_first_frame_must_be_register():
    app = build_hub_app()
    client = TestClient(app)
    with client.websocket_connect("/hub/v1/spoke") as ws:
        ws.send_text(json.dumps({"type": "task_status", "task_id": "t1"}))
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
