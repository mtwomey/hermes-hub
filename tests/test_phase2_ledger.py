"""Phase 2.4 (BEA-305): spoke-side recent-request ledger + prompt injection."""

from __future__ import annotations

import asyncio

from hermes_hub.ledger import RequestLedger, format_recent_requests
from hermes_hub.spoke_executor import SpokeExecutor, build_spoke_prompt


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_ledger_records_start_and_end_with_truncation(tmp_path):
    led = RequestLedger(tmp_path / "l.db", clock=Clock())
    led.record_start(task_id="t1", context_id="c1", caller="Pumpkin", request="r" * 400)
    led.record_end(task_id="t1", state="completed", answer="a" * 900)
    (e,) = led.recent("Pumpkin")
    assert e["task_id"] == "t1" and e["context_id"] == "c1" and e["state"] == "completed"
    assert len(e["request"]) == 300 and len(e["answer"]) == 500


def test_recent_is_caller_scoped_two_hour_window_max_five_newest_first(tmp_path):
    clock = Clock()
    led = RequestLedger(tmp_path / "l.db", clock=clock)
    led.record_start(task_id="old", context_id="c", caller="Pumpkin", request="old one")
    clock.t += 2 * 3600 + 1
    for i in range(7):
        clock.t += 1
        led.record_start(task_id=f"t{i}", context_id="c", caller="Pumpkin", request=f"req {i}")
    led.record_start(task_id="x", context_id="c", caller="Other", request="theirs")
    ids = [e["task_id"] for e in led.recent("Pumpkin")]
    assert ids == ["t6", "t5", "t4", "t3", "t2"]
    assert [e["task_id"] for e in led.recent("Pumpkin", exclude_task_id="t6")][0] == "t5"


def test_prune_drops_entries_older_than_24h(tmp_path):
    clock = Clock()
    led = RequestLedger(tmp_path / "l.db", clock=clock)
    led.record_start(task_id="ancient", context_id="c", caller="P", request="x")
    clock.t += 24 * 3600 + 5
    led.record_start(task_id="fresh", context_id="c", caller="P", request="y")
    led.prune()
    assert led.count() == 1


def test_prompt_includes_recent_block_running_entry_and_instruction():
    entries = [
        {"task_id": "t9", "state": "working", "request": "summarise logs", "answer": "", "age_s": 60},
        {"task_id": "t8", "state": "completed", "request": "count files", "answer": "42 files", "age_s": 600},
    ]
    prompt = build_spoke_prompt(
        spoke_name="Olive", task_id="t10", context_id="c", recent_requests=entries
    )
    assert "Recent requests from this caller (last 2 h)" in prompt
    assert "still running (task t9)" in prompt
    assert "summarise logs" in prompt and "42 files" in prompt
    assert "If this request duplicates one of these" in prompt
    assert "unless the caller explicitly asks to redo it" in prompt


def test_prompt_without_recent_requests_has_no_block():
    assert "Recent requests" not in build_spoke_prompt(spoke_name="O", task_id="t", context_id="c")
    assert format_recent_requests([]) == ""


def test_executor_feeds_ledger_into_next_turn_and_records_outcome(tmp_path):
    led = RequestLedger(tmp_path / "l.db")
    seen = []

    def runner(*, text, session_id, task_id, context_id, spoke_name, recent_requests=None):
        seen.append((task_id, [e["task_id"] for e in (recent_requests or [])]))
        return f"answer to {text}"

    sent = []

    async def send(frame):
        sent.append(frame)

    ex = SpokeExecutor(
        spoke_name="Olive", send=send, agent_runner=runner, ledger=led,
        artifact_root=tmp_path / "art",
    )

    async def go():
        for tid, text in (("t1", "first job"), ("t2", "first job again please")):
            await ex.handle_task_frame(
                {"type": "task", "task_id": tid, "context_id": "c1", "text": text,
                 "metadata": {"callerName": "Pumpkin"}}
            )

    asyncio.run(go())
    assert seen == [("t1", []), ("t2", ["t1"])]
    entries = {e["task_id"]: e for e in led.recent("Pumpkin")}
    assert entries["t1"]["state"] == "completed"
    assert entries["t1"]["answer"] == "answer to first job"


def test_production_spoke_entrypoints_wire_the_ledger():
    """The managed spoke runs scripts/real_spoke.py (services/hermes-spoke-wrapper.sh),
    not cli.py; both must construct SpokeExecutor with the persistent ledger."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("scripts/real_spoke.py", "hermes_hub/cli.py"):
        assert "ledger=RequestLedger(" in (root / rel).read_text(), rel


def test_ledger_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HUB_SPOKE_LEDGER", str(tmp_path / "gate.db"))
    led = RequestLedger()
    assert led.db_path == tmp_path / "gate.db"
