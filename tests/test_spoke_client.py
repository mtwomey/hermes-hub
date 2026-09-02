"""Unit tests for SpokeClient against a minimal local websockets.serve fixture.

No live network needed for this gate's unit tests (per plan Task/Gate 2
text) — a real localhost server started/stopped within the test process.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from hermes_hub.spoke_client import SpokeClient, build_registration_frame


def test_build_registration_frame_shape():
    frame = build_registration_frame(name="Olive", token="secret", skills=[{"id": "x"}])
    assert frame == {
        "type": "register",
        "name": "Olive",
        "token": "secret",
        "skills": [{"id": "x"}],
    }


async def _run_connect_and_register():
    received = []
    served_conn = asyncio.Event()

    async def handler(websocket):
        raw = await websocket.recv()
        received.append(json.loads(raw))
        served_conn.set()
        await websocket.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = SpokeClient(
            hub_url=f"ws://127.0.0.1:{port}/hub/v1/spoke",
            name="Olive",
            token="secret-token",
            skills=[{"id": "general-reasoning"}],
        )
        await client.connect_once()
        await asyncio.wait_for(served_conn.wait(), timeout=5)
        await client.stop()
    return received


def test_client_sends_registration_frame_on_connect():
    received = asyncio.run(_run_connect_and_register())
    assert len(received) == 1
    assert received[0]["type"] == "register"
    assert received[0]["name"] == "Olive"
    assert received[0]["token"] == "secret-token"
    assert received[0]["skills"] == [{"id": "general-reasoning"}]


def test_client_bypasses_environment_proxies_for_lan_websocket(monkeypatch):
    captured = {}

    async def fake_connect(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("stop after inspecting arguments")

    monkeypatch.setattr("hermes_hub.spoke_client.websockets.connect", fake_connect)
    client = SpokeClient(hub_url="ws://192.0.2.236:8770/hub/v1/spoke", name="Olive", token="t")

    with pytest.raises(OSError, match="stop after"):
        asyncio.run(client.connect_once())
    assert captured["proxy"] is None


async def _run_reconnect_with_backoff():
    """Server closes the connection immediately after registering once,
    twice, then accepts and stays open on the third attempt. Assert the
    client retries with increasing backoff, not a tight loop."""
    attempt_count = {"n": 0}
    accepted_third_time = asyncio.Event()

    async def handler(websocket):
        attempt_count["n"] += 1
        await websocket.recv()  # registration frame
        if attempt_count["n"] < 3:
            await websocket.close()
            return
        accepted_third_time.set()
        await websocket.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = SpokeClient(
            hub_url=f"ws://127.0.0.1:{port}/hub/v1/spoke",
            name="Olive",
            token="secret-token",
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.4,
            backoff_multiplier=2.0,
        )
        run_task = asyncio.ensure_future(client.run())
        await asyncio.wait_for(accepted_third_time.wait(), timeout=5)
        await client.stop()
        await run_task
    return client


def test_client_logs_successful_registration(caplog):
    with caplog.at_level("INFO", logger="hermes_hub.spoke_client"):
        asyncio.run(_run_connect_and_register())
    assert "spoke Olive: connected and registered" in caplog.text


def test_client_reconnects_with_increasing_backoff_capped():
    client = asyncio.run(_run_reconnect_with_backoff())
    assert client.connect_attempts == 3
    assert client.registration_frames_sent == 3
    # backoff should have been used twice (after the first two failed
    # attempts), increasing, not a tight loop of zero-delay retries.
    delays = client.backoff_delays_used
    assert len(delays) == 2
    assert delays[0] < delays[1]
    assert all(d > 0 for d in delays)
    assert all(d <= client.max_backoff_seconds for d in delays)
