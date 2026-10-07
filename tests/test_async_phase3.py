"""Phase 3.3 (BEA-306): hub-side cancel. A2A CancelTask and TTL expiry both
send a task_cancel frame to the spoke; the task ends CANCELED."""

from __future__ import annotations

import asyncio

import pytest

from hermes_hub import hub_executor as hub_executor_mod
from hermes_hub.hub_executor import HubExecutor
from hermes_hub.router import Router, TaskTTLExpired

from tests.test_async_phase1 import FakeConnection, FakeContext, FakeUpdater


class CancelUpdater(FakeUpdater):
    async def cancel(self, message=None):
        self.events.append(("cancel", message))


def _patch(monkeypatch, updater, metadata):
    async def fake_open_task(context, event_queue):
        return updater

    monkeypatch.setattr(hub_executor_mod, "open_task", fake_open_task)
    monkeypatch.setattr(hub_executor_mod, "message_metadata", lambda ctx: dict(metadata))


def test_router_cancel_task_sends_task_cancel_to_owning_spoke():
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        agen = router.route_task(spoke_name="Olive", task_id="t1", context_id="c1", text="x", ttl_seconds=5)
        first = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        sent = await router.cancel_task("t1", reason="cancelled")
        await router.dispatch_frame_from_spoke({"type": "task_cancelled", "task_id": "t1", "reason": "cancelled"})
        frame = await first
        return sent, frame

    sent, frame = asyncio.run(go())
    assert sent is True
    assert {"type": "task_cancel", "task_id": "t1", "reason": "cancelled"} in conn.sent
    assert frame["type"] == "task_cancelled"


def test_router_cancel_unknown_task_returns_false():
    router = Router()
    router.register_connection("Olive", FakeConnection())
    assert asyncio.run(router.cancel_task("nope")) is False


def test_ttl_expiry_sends_task_cancel_with_ttl_reason():
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        async for _ in router.route_task(spoke_name="Olive", task_id="t1", context_id="c1", text="x", ttl_seconds=0.1):
            pass

    with pytest.raises(TaskTTLExpired):
        asyncio.run(go())
    assert conn.sent[-1] == {"type": "task_cancel", "task_id": "t1", "reason": "ttl_expired"}


def test_executor_ttl_expiry_ends_canceled_with_ttl_expired(monkeypatch):
    router = Router()
    router.register_connection("Olive", FakeConnection())
    updater = CancelUpdater()
    _patch(monkeypatch, updater, {"targetSpoke": "Olive"})
    asyncio.run(HubExecutor(router=router, ttl_seconds=0.1).execute(FakeContext(), None))
    kind, message = updater.events[-1]
    assert kind == "cancel"
    assert message["metadata"] == {"hermesError": "ttl_expired"}


def test_executor_maps_spoke_task_cancelled_to_canceled(monkeypatch):
    router = Router()

    class AckConnection(FakeConnection):
        async def send(self, frame):
            await super().send(frame)
            if frame.get("type") == "task":
                asyncio.get_running_loop().call_later(
                    0.02,
                    lambda: asyncio.ensure_future(router.dispatch_frame_from_spoke(
                        {"type": "task_cancelled", "task_id": frame["task_id"], "reason": "cancelled"})),
                )

    router.register_connection("Olive", AckConnection())
    updater = CancelUpdater()
    _patch(monkeypatch, updater, {"targetSpoke": "Olive"})
    asyncio.run(HubExecutor(router=router, ttl_seconds=5).execute(FakeContext(), None))
    kind, message = updater.events[-1]
    assert kind == "cancel"
    assert message["metadata"] == {"hermesError": "cancelled"}


def test_executor_cancel_sends_task_cancel_then_marks_canceled(monkeypatch):
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)
    updater = CancelUpdater()
    _patch(monkeypatch, updater, {"targetSpoke": "Olive"})
    executor = HubExecutor(router=router, ttl_seconds=5)

    async def go():
        run = asyncio.ensure_future(executor.execute(FakeContext(), None))
        await asyncio.sleep(0.05)
        await executor.cancel(FakeContext(), None)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(go())
    assert {"type": "task_cancel", "task_id": "t1", "reason": "cancelled"} in conn.sent
    assert ("cancel", {"text": "Cancelled by caller.", "metadata": {"hermesError": "cancelled"}}) in updater.events
