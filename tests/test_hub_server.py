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


def test_external_a2a_route_rejects_missing_bearer_token_when_configured():
    app = build_hub_app(expected_external_token="secret-external-token")
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 401


def test_external_a2a_route_rejects_wrong_bearer_token_when_configured():
    app = build_hub_app(expected_external_token="secret-external-token")
    client = TestClient(app)
    resp = client.get(
        "/.well-known/agent-card.json", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


def test_external_a2a_route_accepts_correct_bearer_token_when_configured():
    app = build_hub_app(expected_external_token="secret-external-token")
    client = TestClient(app)
    resp = client.get(
        "/.well-known/agent-card.json",
        headers={"Authorization": "Bearer secret-external-token"},
    )
    assert resp.status_code == 200


def test_external_a2a_route_allows_any_request_when_no_token_configured():
    """Dev mode: an empty expected_external_token means auth is not enforced,
    matching hermes-peer's D5 behavior."""
    app = build_hub_app()
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200


def test_external_auth_does_not_gate_the_spoke_websocket_route():
    """The spoke WebSocket endpoint has its own token check (expected_spoke_token);
    the external HTTP bearer middleware must not additionally block it."""
    app = build_hub_app(expected_external_token="secret-external-token")
    client = TestClient(app)
    with client.websocket_connect("/hub/v1/spoke") as ws:
        ws.send_text(json.dumps({"type": "register", "name": "Olive", "token": "", "skills": []}))
        resp = client.get(
            "/health", headers={"Authorization": "Bearer secret-external-token"}
        )
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


def test_artifact_download_requires_external_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    from hermes_hub.artifacts import store_artifact_bytes

    ref = store_artifact_bytes(task_id="t1", name="blob.bin", data=b"hello")

    app = build_hub_app(expected_external_token="secret-external-token")
    client = TestClient(app)
    resp = client.get(f"/a2a/artifacts/t1/{ref.artifact_id}")
    assert resp.status_code == 401


def test_artifact_download_returns_bytes_with_correct_auth(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    from hermes_hub.artifacts import store_artifact_bytes

    ref = store_artifact_bytes(task_id="t1", name="blob.bin", data=b"hello world")

    app = build_hub_app(expected_external_token="secret-external-token")
    client = TestClient(app)
    resp = client.get(
        f"/a2a/artifacts/t1/{ref.artifact_id}",
        headers={"Authorization": "Bearer secret-external-token"},
    )
    assert resp.status_code == 200
    assert resp.content == b"hello world"


def test_artifact_download_404_for_unknown_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    app = build_hub_app()
    client = TestClient(app)
    resp = client.get("/a2a/artifacts/nonexistent-task/nonexistent-artifact")
    assert resp.status_code == 404


def test_small_inline_artifact_carries_real_bytes_to_external_caller(tmp_path, monkeypatch):
    """Task 2.4 regression: a small inline task_artifact frame must relay
    its actual bytes (base64 raw Part) to the external A2A caller, not just
    sha256/name metadata with an empty text part. Found live during Gate 2
    when `hermes-hub ask` printed a sha256 but no url and no way to recover
    the bytes for a small artifact."""
    import asyncio
    import base64
    import json as _json
    import threading

    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)

    from hermes_hub.registry import SpokeRegistry
    from hermes_hub.router import Router

    registry = SpokeRegistry()
    router = Router(base_url="http://127.0.0.1:8770")
    app = build_hub_app(registry=registry, router=router)
    client = TestClient(app)

    payload = b"small artifact contents"

    with client.websocket_connect("/hub/v1/spoke") as ws:
        ws.send_text(
            _json.dumps({"type": "register", "name": "Olive", "token": "", "skills": []})
        )

        result_holder = {}

        def send_ask():
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "role": "ROLE_USER",
                        "parts": [{"text": "write a file"}],
                        "messageId": "m1",
                        "metadata": {"targetSpoke": "Olive"},
                    }
                },
            }
            with client.stream(
                "POST", "/a2a/v1", json=body, headers={"A2A-Version": "1.0"}
            ) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        result_holder.setdefault("lines", []).append(
                            _json.loads(line[len("data:") :].strip())
                        )

        asker = threading.Thread(target=send_ask)
        asker.start()

        # Give the request a moment to register its task queue, then push
        # the spoke's frames for that task over the websocket.
        import time

        time.sleep(0.2)
        task_id = ws.receive_text()  # the routed "task" frame sent to the spoke
        task_id = _json.loads(task_id)["task_id"]

        ws.send_text(
            _json.dumps(
                {
                    "type": "task_artifact",
                    "task_id": task_id,
                    "artifact_id": "art1",
                    "name": "small.txt",
                    "data": base64.b64encode(payload).decode("ascii"),
                    "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                    "mime_type": "text/plain",
                }
            )
        )
        ws.send_text(
            _json.dumps({"type": "task_complete", "task_id": task_id, "text": "done"})
        )
        asker.join(timeout=5)

    artifact_events = [
        e
        for e in result_holder.get("lines", [])
        if "artifactUpdate" in e.get("result", {})
    ]
    assert len(artifact_events) == 1
    artifact = artifact_events[0]["result"]["artifactUpdate"]["artifact"]
    parts = artifact["parts"]
    raw_parts = [p for p in parts if p.get("raw")]
    assert len(raw_parts) == 1
    assert base64.b64decode(raw_parts[0]["raw"]) == payload
