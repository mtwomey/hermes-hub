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
            "credential": "",
        }
    ]
    assert frames == [{"type": "task_complete", "task_id": "t1", "text": "25"}]


def test_router_relays_credential_verbatim():
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "ok"}
            )

        asyncio.ensure_future(fake_spoke_worker())
        await _collect_task_frames(
            router,
            spoke_name="Olive",
            task_id="t1",
            context_id="c1",
            text="hi",
            credential="opaque-secret-value",
        )

    asyncio.run(go())
    assert conn.sent[0]["credential"] == "opaque-secret-value"


def test_router_does_not_store_credential():
    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "ok"}
            )

        asyncio.ensure_future(fake_spoke_worker())
        await _collect_task_frames(
            router,
            spoke_name="Olive",
            task_id="t1",
            context_id="c1",
            text="hi",
            credential="opaque-secret-value",
        )

    asyncio.run(go())
    # After the task completes, the credential must not persist anywhere in
    # router state: no attribute, no queue entry, no cache.
    router_state = repr(vars(router))
    assert "opaque-secret-value" not in router_state


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


def test_route_task_sends_inbound_file_as_chunked_frames_before_task_frame():
    """Task 2.5: an inbound file (caller -> spoke) is relayed as
    artifact_begin/chunk*/end frames sent to the spoke BEFORE the task
    frame, so the spoke can write it to disk before the agent runs."""
    import hashlib

    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    payload = b"inbound file contents"
    digest = hashlib.sha256(payload).hexdigest()

    async def go():
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "used the file"}
            )

        asyncio.ensure_future(fake_spoke_worker())
        return await _collect_task_frames(
            router,
            spoke_name="Olive",
            task_id="t1",
            context_id="c1",
            text="use this file",
            inbound_file={"name": "input.txt", "mime_type": "text/plain", "data": payload},
        )

    asyncio.run(go())

    types_in_order = [f["type"] for f in conn.sent]
    assert types_in_order[0] == "artifact_begin"
    assert types_in_order[-2] == "artifact_end"
    assert types_in_order[-1] == "task"  # the task frame goes out last
    begin_frame = conn.sent[0]
    assert begin_frame["sha256"] == digest
    assert begin_frame["name"] == "input.txt"


def test_dispatch_frame_without_task_id_is_ignored_not_crash():
    router = Router()

    async def go():
        await router.dispatch_frame_from_spoke({"type": "register", "name": "Olive"})

    asyncio.run(go())  # must not raise


def test_credential_never_logged(caplog):
    """Route a task with a canary credential and assert it never appears in
    logs emitted anywhere on the routing path (Task 1.3, V5)."""
    import logging

    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "ok"}
            )

        asyncio.ensure_future(fake_spoke_worker())
        await _collect_task_frames(
            router,
            spoke_name="Olive",
            task_id="t1",
            context_id="c1",
            text="hi",
            credential="SUPERSECRET-CANARY",
        )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())

    assert "SUPERSECRET-CANARY" not in caplog.text


def test_router_reassembles_chunked_artifact_and_serves_url(tmp_path, monkeypatch):
    """Task 2.4: the router buffers artifact_begin/chunk/end frames from the
    spoke, verifies the declared SHA-256, stores via artifacts.py, and
    yields a single synthesized task_artifact frame carrying a download URL
    instead of forwarding the raw chunk frames to the caller."""
    import hashlib

    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)

    from hermes_hub.protocol import (
        build_artifact_begin_frame,
        build_artifact_chunk_frame,
        build_artifact_end_frame,
        chunk_artifact_bytes,
    )

    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    payload = bytes(range(256)) * 2000
    digest = hashlib.sha256(payload).hexdigest()

    async def go():
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                build_artifact_begin_frame(
                    task_id="t1",
                    artifact_id="a1",
                    name="blob.bin",
                    mime_type="application/octet-stream",
                    total_bytes=len(payload),
                    sha256=digest,
                )
            )
            for seq, chunk in enumerate(chunk_artifact_bytes(payload)):
                await router.dispatch_frame_from_spoke(
                    build_artifact_chunk_frame(task_id="t1", artifact_id="a1", seq=seq, data=chunk)
                )
            await router.dispatch_frame_from_spoke(
                build_artifact_end_frame(task_id="t1", artifact_id="a1")
            )
            await router.dispatch_frame_from_spoke(
                {"type": "task_complete", "task_id": "t1", "text": "done"}
            )

        asyncio.ensure_future(fake_spoke_worker())
        return await _collect_task_frames(
            router,
            spoke_name="Olive",
            task_id="t1",
            context_id="c1",
            text="write a file",
            timeout_seconds=5,
        )

    frames = asyncio.run(go())
    artifact_frames = [f for f in frames if f["type"] == "task_artifact"]
    assert len(artifact_frames) == 1
    assert artifact_frames[0]["sha256"] == digest
    assert "url" in artifact_frames[0]
    assert artifact_frames[0]["url"].endswith("/t1/a1")
    # The raw chunk frames must not leak through to the caller.
    assert not [f for f in frames if f["type"] in ("artifact_begin", "artifact_chunk", "artifact_end")]
    assert frames[-1]["type"] == "task_complete"


def test_router_fails_task_on_sha256_mismatch(tmp_path, monkeypatch):
    """Task 2.4: a deliberately corrupted chunk must fail the task with a
    hash-mismatch error, not silently complete."""
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)

    from hermes_hub.protocol import (
        build_artifact_begin_frame,
        build_artifact_chunk_frame,
        build_artifact_end_frame,
    )

    router = Router()
    conn = FakeConnection()
    router.register_connection("Olive", conn)

    async def go():
        async def fake_spoke_worker():
            await asyncio.sleep(0.01)
            await router.dispatch_frame_from_spoke(
                build_artifact_begin_frame(
                    task_id="t1",
                    artifact_id="a1",
                    name="blob.bin",
                    mime_type="application/octet-stream",
                    total_bytes=4,
                    sha256="0" * 64,  # wrong on purpose
                )
            )
            await router.dispatch_frame_from_spoke(
                build_artifact_chunk_frame(task_id="t1", artifact_id="a1", seq=0, data=b"AAAA")
            )
            await router.dispatch_frame_from_spoke(
                build_artifact_end_frame(task_id="t1", artifact_id="a1")
            )

        asyncio.ensure_future(fake_spoke_worker())
        return await _collect_task_frames(
            router,
            spoke_name="Olive",
            task_id="t1",
            context_id="c1",
            text="write a file",
            timeout_seconds=5,
        )

    frames = asyncio.run(go())
    assert frames[-1]["type"] == "task_failed"
    assert "hash" in frames[-1]["error"].lower() or "sha-256" in frames[-1]["error"].lower()