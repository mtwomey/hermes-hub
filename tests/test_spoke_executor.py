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
