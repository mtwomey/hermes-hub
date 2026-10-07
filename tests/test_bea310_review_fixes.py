"""BEA-310: fixes from the Olive real-world interaction review.

1. Ledger excerpts are marked as excerpts and the agent is told the hub holds
   the full result (the spoke told a caller the rest "wasn't saved").
2. One process-wide SessionDB instead of one leaked handle per hub turn.
3. "Do not modify anything" requests are explicitly read-only in the prompt.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import types

from hermes_hub import spoke_executor
from hermes_hub.ledger import RequestLedger, format_recent_requests
from hermes_hub.spoke_executor import build_spoke_prompt


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_long_answer_is_marked_as_excerpt_with_total_length(tmp_path):
    led = RequestLedger(tmp_path / "l.db", clock=Clock())
    led.record_start(task_id="t1", context_id="c1", caller="default", request="check email")
    led.record_end(task_id="t1", state="completed", answer="a" * 1793)
    (e,) = led.recent("default")
    assert len(e["answer"]) == 500 and e["answer_len"] == 1793
    block = format_recent_requests([e])
    assert "[EXCERPT: first 500 of 1793 chars]" in block
    assert "GetTask" in block and "peer_status" in block
    assert "Never say the rest of an earlier answer was lost" in block


def test_short_answer_is_not_marked(tmp_path):
    led = RequestLedger(tmp_path / "l.db", clock=Clock())
    led.record_start(task_id="t1", context_id="c1", caller="default", request="ping")
    led.record_end(task_id="t1", state="completed", answer="pong")
    (e,) = led.recent("default")
    assert "EXCERPT" not in format_recent_requests([e])


def test_pre_bea310_ledger_is_migrated_in_place(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE requests (task_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, "
        "caller TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL, "
        "answer TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO requests VALUES ('old', 'c', 'default', 'r', 'completed', 'x', ?, ?)",
        (999_999.0, 999_999.0),
    )
    conn.commit()
    conn.close()
    led = RequestLedger(db, clock=Clock())
    led.record_start(task_id="new", context_id="c", caller="default", request="r2")
    led.record_end(task_id="new", state="completed", answer="b" * 600)
    by_id = {e["task_id"]: e for e in led.recent("default")}
    assert by_id["old"]["answer_len"] == 0 and by_id["new"]["answer_len"] == 600
    assert "EXCERPT" in format_recent_requests([by_id["new"]])
    assert "EXCERPT" not in format_recent_requests([by_id["old"]])


def test_session_db_is_one_shared_instance(monkeypatch):
    made = []

    class FakeSessionDB:
        def __init__(self):
            made.append(self)

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=FakeSessionDB))
    monkeypatch.setattr(spoke_executor, "_SESSION_DB", None)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(spoke_executor._open_session_db()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1
    assert all(r is made[0] for r in results)


def test_session_db_unavailable_returns_none_and_retries_later(monkeypatch):
    monkeypatch.setattr(spoke_executor, "_SESSION_DB", None)
    monkeypatch.setitem(sys.modules, "hermes_state", None)  # import fails
    assert spoke_executor._open_session_db() is None

    class FakeSessionDB:
        pass

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=FakeSessionDB))
    assert isinstance(spoke_executor._open_session_db(), FakeSessionDB)


def test_prompt_makes_do_not_modify_requests_read_only():
    prompt = build_spoke_prompt(spoke_name="Olive", task_id="t", context_id="c")
    assert "strictly read-only" in prompt
    assert "skills" in prompt and "install" in prompt


def test_hub_client_stamps_caller_name(monkeypatch):
    from hermes_hub import hub_client

    c = hub_client.HubClient(hub_url="http://hub")
    kw = dict(context_id="", credential="", file_name="", file_bytes=None, file_mime_type="")
    monkeypatch.setenv(hub_client.ENV_CALLER_NAME, "Olive-copilot")
    assert c._build_message("Olive", "hi", **kw)["metadata"]["callerName"] == "Olive-copilot"
    monkeypatch.delenv(hub_client.ENV_CALLER_NAME)
    monkeypatch.setattr(hub_client.socket, "gethostname", lambda: "Pumpkin.local")
    assert c._build_message("Olive", "hi", **kw)["metadata"]["callerName"] == "Pumpkin"
    monkeypatch.setattr(hub_client.socket, "gethostname", lambda: "")
    assert "callerName" not in c._build_message("Olive", "hi", **kw)["metadata"]
