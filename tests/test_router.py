"""Router unit tests, including proving frames arrive incrementally
(not buffered) as they're dispatched from a fake spoke connection."""

from __future__ import annotations

import asyncio
import time

import pytest

from hermes_hub.router import Router, SpokeUnavailableError


class FakeConnection:
    """A fake live spoke connection whose .send() records what was sent."""

    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)


async def _collect_task_frames(router: Router, **kwargs):
    frames = []
    async for frame in router.route_task(**kwargs):
        frames.append(frame)
    return frames


def test_route_task_raises_when_spoke_not_connected():
    router = Router()

    async def go():
        with pytest.raises(SpokeUnavailableError):
            async for _ in router.route_task(
                spoke_name="Ghost", task_id="t1", context_id="c1", text="hi"
            ):
                pass

    asyncio.run(go())


def test_route_task_sends_task_frame_to_the_right_connection():
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        # Simulate the spoke immediately completing, from a background task,
        # so route_task's generator can finish.
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "25"}
            )

        asyncio.ensure_future(fake_spoke_worker())
        frames = await _collect_task_frames(
            router, spoke_name="Olive", task_id="t1", context_id="c1", text="What is 9+16?"
        )
        return frames

    frames = asyncio.run(go())
    assert conn.sent == [
        {
            "type": "task",
            "task_id": "t1",
            "context_id": "c1",
            "text": "What is 9+16?",
            "metadata": {},
        }
    ]
    assert frames == [{"type": "task_complete", "task_id": "t1", "text": "25"}]


def test_route_task_yields_frames_incrementally_not_buffered():
    """The hard requirement behind Gate 3: frames must arrive as the spoke
    emits them, not all at once at the end. Prove it with real elapsed time
    between dispatched frames and assert the generator yields each one
    close to when it was dispatched, not all together after the last."""
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    STEP_DELAY = 0.2
    N_STATUS_FRAMES = 3

    async def go():
        yield_times = []

        async def fake_slow_spoke_worker():
            for i in range(N_STATUS_FRAMES):
                await asyncio.sleep(STEP_DELAY)
                await router.dispatch_frame_from_spoke(
                    {"type": "task_status", "task_id": "t1", "state": "working", "seq": i}
                )
            await asyncio.sleep(STEP_DELAY)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "done"}
            )

        asyncio.ensure_future(fake_slow_spoke_worker())

        start = time.monotonic()
        async for frame in router.route_task(
            spoke_name="Olive", task_id="t1", context_id="c1", text="slow task", timeout_seconds=5
        ):
            yield_times.append(time.monotonic() - start)
        return yield_times

    yield_times = asyncio.run(go())
    assert len(yield_times) == N_STATUS_FRAMES + 1
    # Each frame must arrive close to its own dispatch time, not all
    # bunched at the end (which would mean buffering, the Gate 3 failure
    # mode). Assert successive yields are spaced by roughly STEP_DELAY,
    # not all clustered within one short window at the very end.
    for i in range(1, len(yield_times)):
        gap = yield_times[i] - yield_times[i - 1]
        assert gap > STEP_DELAY * 0.5, (
            f"frame {i} arrived only {gap:.3f}s after frame {i - 1}; "
            "frames look buffered/flushed together instead of incremental"
        )
    # And the whole run must have taken close to the full expected duration,
    # not returned instantly (which is the classic "looks streamed but
    # isn't" bug: an implementation that awaits everything before yielding
    # anything would make the first yield_time already ~= total duration).
    total_expected = STEP_DELAY * (N_STATUS_FRAMES + 1)
    assert yield_times[0] < total_expected * 0.7, (
        "first frame did not arrive until nearly the whole task was done; "
        "looks buffered, not incremental"
    )


def test_route_task_times_out_if_spoke_never_responds():
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        with pytest.raises(TimeoutError):
            async for _ in router.route_task(
                spoke_name="Olive", task_id="t1", context_id="c1", text="hi", timeout_seconds=0.1
            ):
                pass

    asyncio.run(go())


def test_dispatch_frame_without_task_id_is_ignored_not_crash():
    router = Router()

    async def go():
        await router.dispatch_frame_from_spoke({"type": "register", "name": "Olive"})

    asyncio.run(go())  # must not raise
