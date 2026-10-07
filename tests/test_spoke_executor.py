"""Unit tests for SpokeExecutor: heartbeats while a slow agent runs, correct
completion/failure frames, and session_id resolution via SessionMap. Uses a
fake ``agent_runner`` (no real Hermes runtime import needed)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from hermes_hub.sessions import SessionMap, SessionStore
from hermes_hub.spoke_executor import SpokeExecutor


def _fresh_session_map(tmp_path):
    return SessionMap(store=SessionStore(db_path=Path(tmp_path) / "sessions.db"))


def test_completes_and_sends_final_answer(tmp_path):
    sent = []

    async def send(frame):
        sent.append(frame)

    def fake_runner(*, text, session_id, task_id, context_id, spoke_name):
        return f"echo:{text}"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=fake_runner,
        heartbeat_interval_seconds=0.05,
    )

    asyncio.run(
        executor.handle_task_frame(
            {"task_id": "t1", "context_id": "c1", "text": "What is 9+16?"}
        )
    )

    assert sent[-1] == {"type": "task_complete", "task_id": "t1", "text": "echo:What is 9+16?"}


def test_sends_heartbeats_while_slow_agent_runs(tmp_path):
    sent = []

    async def send(frame):
        sent.append(frame)

    def slow_runner(*, text, session_id, task_id, context_id, spoke_name):
        time.sleep(0.25)
        return "slow answer"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=slow_runner,
        heartbeat_interval_seconds=0.05,
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "slow"})
    )

    status_frames = [f for f in sent if f["type"] == "task_status"]
    assert len(status_frames) >= 2  # multiple heartbeats during the 0.25s sleep
    assert sent[-1]["type"] == "task_complete"


def test_failure_sends_task_failed_frame(tmp_path):
    sent = []

    async def send(frame):
        sent.append(frame)

    def failing_runner(*, text, session_id, task_id, context_id, spoke_name):
        raise RuntimeError("boom")

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=failing_runner,
        heartbeat_interval_seconds=0.05,
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "hi"})
    )

    assert sent[-1] == {"type": "task_failed", "task_id": "t1", "error": "boom"}


def test_same_context_id_reuses_session_id_across_calls(tmp_path):
    seen_session_ids = []

    async def send(frame):
        pass

    def recording_runner(*, text, session_id, task_id, context_id, spoke_name):
        seen_session_ids.append(session_id)
        return "ok"

    session_map = _fresh_session_map(tmp_path)
    executor = SpokeExecutor(
        spoke_name="Olive", send=send, session_map=session_map, agent_runner=recording_runner
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "turn 1"})
    )
    asyncio.run(
        executor.handle_task_frame({"task_id": "t2", "context_id": "c1", "text": "turn 2"})
    )
    asyncio.run(
        executor.handle_task_frame({"task_id": "t3", "context_id": "c2", "text": "different convo"})
    )

    assert seen_session_ids[0] == seen_session_ids[1]  # same contextId -> same session
    assert seen_session_ids[2] != seen_session_ids[0]  # different contextId -> different session

def test_spoke_rejects_task_with_wrong_credential(tmp_path):
    sent = []

    async def send(frame):
        sent.append(frame)

    def agent_that_must_not_run(*, text, session_id, task_id, context_id, spoke_name):
        raise AssertionError("agent must never be invoked for a wrong credential")

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_must_not_run,
        expected_credential="correct-horse",
    )

    asyncio.run(
        executor.handle_task_frame(
            {
                "task_id": "t1",
                "context_id": "c1",
                "text": "hi",
                "credential": "wrong-value",
            }
        )
    )

    assert len(sent) == 1
    assert sent[0]["type"] == "task_failed"
    assert sent[0]["task_id"] == "t1"
    # Do not echo the presented credential back in the error (Task 1.4).
    assert "wrong-value" not in sent[0]["error"]


def test_spoke_rejects_task_with_missing_credential(tmp_path):
    sent = []

    async def send(frame):
        sent.append(frame)

    def agent_that_must_not_run(*, text, session_id, task_id, context_id, spoke_name):
        raise AssertionError("agent must never be invoked for a missing credential")

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_must_not_run,
        expected_credential="correct-horse",
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "hi"})
    )

    assert sent[-1]["type"] == "task_failed"


def test_spoke_accepts_task_with_correct_credential(tmp_path):
    sent = []
    invoked = []

    async def send(frame):
        sent.append(frame)

    def agent_runner(*, text, session_id, task_id, context_id, spoke_name):
        invoked.append(text)
        return f"echo:{text}"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_runner,
        expected_credential="correct-horse",
    )

    asyncio.run(
        executor.handle_task_frame(
            {
                "task_id": "t1",
                "context_id": "c1",
                "text": "hi",
                "credential": "correct-horse",
            }
        )
    )

    assert invoked == ["hi"]
    assert sent[-1] == {"type": "task_complete", "task_id": "t1", "text": "echo:hi"}


def test_spoke_dev_mode_allows_when_no_secret_configured(tmp_path):
    """Mirrors hermes-peer D5 / hub._check_token: unset expected credential
    means dev mode -- allow regardless of what's presented."""
    sent = []
    invoked = []

    async def send(frame):
        sent.append(frame)

    def agent_runner(*, text, session_id, task_id, context_id, spoke_name):
        invoked.append(text)
        return "ok"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_runner,
        expected_credential="",
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "hi"})
    )

    assert invoked == ["hi"]
    assert sent[-1]["type"] == "task_complete"


def test_spoke_credential_never_logged(tmp_path, caplog):
    """Canary: a wrong-credential rejection must not write the credential
    value into any log record."""
    import logging

    async def send(frame):
        pass

    def agent_that_must_not_run(*, text, session_id, task_id, context_id, spoke_name):
        raise AssertionError("agent must never be invoked")

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_must_not_run,
        expected_credential="correct-horse",
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            executor.handle_task_frame(
                {
                    "task_id": "t1",
                    "context_id": "c1",
                    "text": "hi",
                    "credential": "SUPERSECRET-CANARY",
                }
            )
        )

    assert "SUPERSECRET-CANARY" not in caplog.text
    assert "correct-horse" not in caplog.text


def test_spoke_emits_produced_artifact_inline_when_small(tmp_path):
    """Task 2.3: the spoke's own output directory is scanned after the
    agent turn; a small produced file goes out inline (task_artifact)."""
    sent = []

    async def send(frame):
        sent.append(frame)

    def agent_that_writes_a_file(*, text, session_id, task_id, context_id, spoke_name, output_dir):
        (output_dir / "result.txt").write_bytes(b"small output")
        return "done"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_writes_a_file,
        artifact_root=tmp_path / "artifacts",
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "write a file"})
    )

    artifact_frames = [f for f in sent if f["type"] == "task_artifact"]
    assert len(artifact_frames) == 1
    assert artifact_frames[0]["name"] == "result.txt"
    import base64

    assert base64.b64decode(artifact_frames[0]["data"]) == b"small output"
    # task_complete must still be the final frame.
    assert sent[-1]["type"] == "task_complete"


def test_spoke_emits_produced_artifact_chunked_when_large(tmp_path):
    """Task 2.3: a file over INLINE_MAX_BYTES goes out as
    artifact_begin/artifact_chunk*/artifact_end instead of inline."""
    sent = []

    async def send(frame):
        sent.append(frame)

    big_payload = bytes(range(256)) * 2000  # ~512KB, well over the threshold

    def agent_that_writes_a_big_file(*, text, session_id, task_id, context_id, spoke_name, output_dir):
        (output_dir / "blob.bin").write_bytes(big_payload)
        return "done"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_writes_a_big_file,
        artifact_root=tmp_path / "artifacts",
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "write a big file"})
    )

    begin_frames = [f for f in sent if f["type"] == "artifact_begin"]
    chunk_frames = [f for f in sent if f["type"] == "artifact_chunk"]
    end_frames = [f for f in sent if f["type"] == "artifact_end"]
    assert len(begin_frames) == 1
    assert begin_frames[0]["total_bytes"] == len(big_payload)
    import hashlib

    assert begin_frames[0]["sha256"] == hashlib.sha256(big_payload).hexdigest()
    assert len(chunk_frames) > 1  # must actually span multiple chunks
    assert len(end_frames) == 1
    # No inline task_artifact frame for this large file.
    assert not [f for f in sent if f["type"] == "task_artifact"]

    from hermes_hub.protocol import reassemble_artifact_chunks

    assert reassemble_artifact_chunks(chunk_frames) == big_payload


def test_spoke_reassembles_inbound_file_before_running_agent(tmp_path):
    """Task 2.5: artifact_begin/chunk*/end frames received before the task
    frame are reassembled and written to the task's input directory; the
    agent runner receives the file's path."""
    import hashlib

    sent = []

    async def send(frame):
        sent.append(frame)

    captured = {}

    def agent_that_reads_input_file(
        *, text, session_id, task_id, context_id, spoke_name, output_dir, input_files
    ):
        captured["input_file_names"] = [p.name for p in input_files]
        captured["input_file_bytes"] = [p.read_bytes() for p in input_files]
        return "read the file: " + input_files[0].read_bytes().decode()

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_reads_input_file,
        artifact_root=tmp_path / "artifacts",
    )

    from hermes_hub.protocol import (
        build_artifact_begin_frame,
        build_artifact_chunk_frame,
        build_artifact_end_frame,
    )

    payload = b"hello from the caller"
    digest = hashlib.sha256(payload).hexdigest()

    async def _drive():
        await executor.handle_frame(
            build_artifact_begin_frame(
                task_id="t1",
                artifact_id="inbound_t1",
                name="input.txt",
                mime_type="text/plain",
                total_bytes=len(payload),
                sha256=digest,
            )
        )
        await executor.handle_frame(
            build_artifact_chunk_frame(task_id="t1", artifact_id="inbound_t1", seq=0, data=payload)
        )
        await executor.handle_frame(build_artifact_end_frame(task_id="t1", artifact_id="inbound_t1"))
        await executor.handle_frame(
            {"task_id": "t1", "context_id": "c1", "text": "use the file", "type": "task"}
        )
        await executor.drain()

    asyncio.run(_drive())

    assert len(captured["input_file_names"]) == 1
    assert captured["input_file_names"][0] == "input.txt"
    assert captured["input_file_bytes"][0] == payload
    assert sent[-1]["type"] == "task_complete"
    assert sent[-1]["text"] == "read the file: hello from the caller"


def test_spoke_cleans_output_directory_after_task(tmp_path):
    sent = []

    async def send(frame):
        sent.append(frame)

    captured_dir = {}

    def agent_that_writes_a_file(*, text, session_id, task_id, context_id, spoke_name, output_dir):
        captured_dir["path"] = output_dir
        (output_dir / "result.txt").write_bytes(b"data")
        return "done"

    executor = SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=agent_that_writes_a_file,
        artifact_root=tmp_path / "artifacts",
    )

    asyncio.run(
        executor.handle_task_frame({"task_id": "t1", "context_id": "c1", "text": "write a file"})
    )

    assert not captured_dir["path"].exists()


# --- Phase 3.2: background dispatch + cancel (BEA-306) ----------------------

import logging
import threading


def _cancel_executor(tmp_path, runner, sent, **kw):
    async def send(frame):
        sent.append(frame)

    return SpokeExecutor(
        spoke_name="Olive",
        send=send,
        session_map=_fresh_session_map(tmp_path),
        agent_runner=runner,
        heartbeat_interval_seconds=0.05,
        artifact_root=Path(tmp_path) / "artifacts",
        **kw,
    )


def test_task_frame_dispatch_does_not_block_receive_loop(tmp_path):
    """A running task must not stop the spoke from reading further frames."""
    sent = []
    release = threading.Event()

    def blocked_runner(*, text, session_id, task_id, context_id, spoke_name):
        release.wait(5)
        return f"done:{task_id}"

    executor = _cancel_executor(tmp_path, blocked_runner, sent)

    async def _drive():
        t0 = time.monotonic()
        await executor.handle_frame({"type": "task", "task_id": "a", "context_id": "c", "text": "x"})
        await executor.handle_frame({"type": "task", "task_id": "b", "context_id": "c2", "text": "y"})
        returned_in = time.monotonic() - t0
        await asyncio.sleep(0.15)
        release.set()
        await executor.drain()
        return returned_in

    returned_in = asyncio.run(_drive())
    assert returned_in < 0.5
    completes = sorted(f["task_id"] for f in sent if f["type"] == "task_complete")
    assert completes == ["a", "b"]


def test_cancel_interrupts_running_agent_and_discards_result(tmp_path, caplog):
    sent = []
    interrupted = []

    class FakeAgent:
        def __init__(self):
            self.stop = threading.Event()

    def runner(*, text, session_id, task_id, context_id, spoke_name, on_agent=None):
        agent = FakeAgent()
        on_agent(agent)
        agent.stop.wait(5)
        return "late answer that must be discarded"

    def interrupter(agent):
        interrupted.append(agent)
        agent.stop.set()

    executor = _cancel_executor(tmp_path, runner, sent, interrupter=interrupter)

    async def _drive():
        await executor.handle_frame({"type": "task", "task_id": "t1", "context_id": "c", "text": "slow"})
        await asyncio.sleep(0.15)
        await executor.handle_frame({"type": "task_cancel", "task_id": "t1", "reason": "cancelled"})
        await executor.drain()

    with caplog.at_level(logging.INFO, logger="hermes_hub.spoke_executor"):
        asyncio.run(_drive())

    assert len(interrupted) == 1
    types = [f["type"] for f in sent if f.get("task_id") == "t1"]
    assert "task_cancelled" in types
    assert "task_complete" not in types and "task_failed" not in types
    assert types[-1] == "task_cancelled"
    assert any("agent interrupted" in r.getMessage() for r in caplog.records)


def test_cancel_without_agent_handle_still_discards(tmp_path):
    sent = []
    release = threading.Event()

    def runner(*, text, session_id, task_id, context_id, spoke_name):
        release.wait(5)
        return "late"

    executor = _cancel_executor(tmp_path, runner, sent)

    async def _drive():
        await executor.handle_frame({"type": "task", "task_id": "t2", "context_id": "c", "text": "s"})
        await asyncio.sleep(0.1)
        await executor.handle_frame({"type": "task_cancel", "task_id": "t2"})
        release.set()
        await executor.drain()

    asyncio.run(_drive())
    types = [f["type"] for f in sent if f.get("task_id") == "t2"]
    assert types.count("task_cancelled") == 1
    assert "task_complete" not in types


def test_cancel_for_unknown_task_acknowledges(tmp_path):
    sent = []
    executor = _cancel_executor(tmp_path, lambda **k: "x", sent)
    asyncio.run(executor.handle_frame({"type": "task_cancel", "task_id": "ghost"}))
    assert sent == [{"type": "task_cancelled", "task_id": "ghost", "reason": "cancelled"}]


def test_malformed_cancel_frame_is_ignored(tmp_path):
    sent = []
    executor = _cancel_executor(tmp_path, lambda **k: "x", sent)
    asyncio.run(executor.handle_frame({"type": "task_cancel"}))
    assert sent == []


def test_real_runner_exposes_on_agent_hook():
    import inspect as _inspect
    from hermes_hub.spoke_executor import run_real_hermes_turn

    assert "on_agent" in _inspect.signature(run_real_hermes_turn).parameters
