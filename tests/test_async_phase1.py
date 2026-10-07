"""Phase 1 (BEA-304): always-async core -- TTL replaces the failure timeout,
late frames are never silently dropped, spoke disconnect fails promptly."""

from __future__ import annotations

import asyncio
import logging

import pytest

from hermes_hub import hub_executor as hub_executor_mod
from hermes_hub import hub_runtime
from hermes_hub.hub_executor import HubExecutor
from hermes_hub.router import Router, TaskTTLExpired


class FakeConnection:
    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)


# ---- 1.1 TTL config -------------------------------------------------------


def _runtime(monkeypatch, **env):
    for key in ("HERMES_HUB_TASK_TTL_SECONDS", "HERMES_HUB_TASK_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hub_runtime, "require_hub_credentials", lambda: ("x", "y"))
    return hub_runtime.resolve_hub_runtime()


def test_task_ttl_defaults_to_1800_seconds(monkeypatch):
    assert _runtime(monkeypatch).task_ttl_seconds == 1800.0


def test_task_ttl_env_var_wins(monkeypatch):
    rt = _runtime(
        monkeypatch,
        HERMES_HUB_TASK_TTL_SECONDS="42",
        HERMES_HUB_TASK_TIMEOUT_SECONDS="7",
    )
    assert rt.task_ttl_seconds == 42.0


def test_old_timeout_env_var_is_deprecated_alias(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        rt = _runtime(monkeypatch, HERMES_HUB_TASK_TIMEOUT_SECONDS="900")
    assert rt.task_ttl_seconds == 900.0
    assert "deprecated" in caplog.text.lower()


# ---- 1.1 slow task completes; TTL -> ttl_expired ---------------------------


def test_task_slower_than_old_timeout_still_completes():
    """A spoke answering after longer than a (simulated) old timeout still
    completes when the TTL is longer."""
    router = Router()
    router.register_connection("Olive", FakeConnection())

    async def go():
        async def late_answer():
            await asyncio.sleep(0.3)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "late but real"}
            )

        asyncio.ensure_future(late_answer())
        frames = []
        async for frame in router.route_task(
            spoke_name="Olive", task_id="t1", context_id="c1", text="slow", ttl_seconds=2.0
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(go())
    assert frames[-1]["text"] == "late but real"


def test_ttl_expiry_raises_task_ttl_expired():
    router = Router()
    router.register_connection("Olive", FakeConnection())

    async def go():
        async for _ in router.route_task(
            spoke_name="Olive", task_id="t1", context_id="c1", text="x", ttl_seconds=0.1
        ):
            pass

    with pytest.raises(TaskTTLExpired):
        asyncio.run(go())


class FakeUpdater:
    def __init__(self):
        self.events = []

    async def start_work(self):
        self.events.append(("start_work", None))

    async def update_status(self, state, message=None, metadata=None, **_):
        self.events.append(("status", metadata))

    def new_agent_message(self, parts, metadata=None):
        return {"text": parts[0].text, "metadata": metadata}

    async def failed(self, message=None):
        self.events.append(("failed", message))

    async def complete(self, message=None):
        self.events.append(("complete", message))

    async def add_artifact(self, *a, **k):
        self.events.append(("artifact", k))


class FakeContext:
    task_id = "t1"
    context_id = "c1"
    current_task = None
    message = None

    def get_user_input(self):
        return "hi"


def _run_executor(monkeypatch, router, metadata, ttl):
    updater = FakeUpdater()

    async def fake_open_task(context, event_queue):
        return updater

    monkeypatch.setattr(hub_executor_mod, "open_task", fake_open_task)
    monkeypatch.setattr(hub_executor_mod, "message_metadata", lambda ctx: dict(metadata))
    executor = HubExecutor(router=router, ttl_seconds=ttl)
    asyncio.run(executor.execute(FakeContext(), None))
    return updater


def test_executor_ttl_expiry_is_ttl_expired_not_timeout(monkeypatch):
    router = Router()
    router.register_connection("Olive", FakeConnection())
    updater = _run_executor(monkeypatch, router, {"targetSpoke": "Olive"}, ttl=0.1)
    kind, message = updater.events[-1]
    assert kind == "failed"
    assert message["metadata"] == {"hermesError": "ttl_expired"}


def test_executor_default_ttl_is_1800():
    assert HubExecutor(router=Router()).ttl_seconds == 1800.0


# ---- 1.2 late frames ------------------------------------------------------


def test_late_frame_for_unknown_task_is_logged_and_counted(caplog):
    router = Router()

    async def go():
        await router.dispatch_frame_from_spoke(
            {"type": "task_complete", "task_id": "gone", "text": "SECRET-PAYLOAD"}
        )

    with caplog.at_level(logging.WARNING, logger="hermes_hub.router"):
        asyncio.run(go())
    assert router.late_frame_count == 1
    assert "gone" in caplog.text and "task_complete" in caplog.text
    assert "SECRET-PAYLOAD" not in caplog.text


# ---- risk list: spoke disconnect mid-task --------------------------------


def test_spoke_disconnect_mid_task_fails_promptly(monkeypatch):
    router = Router()
    router.register_connection("Olive", FakeConnection())

    async def go():
        async def drop():
            await asyncio.sleep(0.05)
            router.unregister_connection("Olive")

        asyncio.ensure_future(drop())
        frames = []
        async for frame in router.route_task(
            spoke_name="Olive", task_id="t1", context_id="c1", text="x", ttl_seconds=5
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(go())
    assert frames[-1]["type"] == "task_failed"
    assert frames[-1]["hermes_error"] == "spoke_disconnected"


def test_executor_maps_spoke_disconnect_to_hermes_error(monkeypatch):
    router = Router()
    router.register_connection("Olive", FakeConnection())

    async def drop_soon():
        await asyncio.sleep(0.05)
        router.unregister_connection("Olive")

    original = router.route_task

    async def route_and_drop(**kwargs):
        asyncio.ensure_future(drop_soon())
        async for frame in original(**kwargs):
            yield frame

    router.route_task = route_and_drop
    updater = _run_executor(monkeypatch, router, {"targetSpoke": "Olive"}, ttl=5)
    kind, message = updater.events[-1]
    assert kind == "failed"
    assert message["metadata"] == {"hermesError": "spoke_disconnected"}
