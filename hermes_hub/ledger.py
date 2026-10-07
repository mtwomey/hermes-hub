"""Phase 2.4 (BEA-305): spoke-side recent-request ledger (duplicate layer b).

A small SQLite table next to ``spoke_sessions.db`` recording, per routed
task: task_id, context_id, caller label, first 300 chars of the request,
state, first 500 chars of the answer and timestamps. Rows older than 24 h are
pruned. Before each agent turn the spoke shows the agent the caller's last
2 h of requests (max 5) so a rephrased duplicate can be answered by
referring to the earlier result instead of redoing the work.

Privacy: the ledger never leaves the spoke; only the agent's answer does.
Caller label = task-frame ``metadata.callerName`` when the caller sends one,
else ``"default"`` (today each spoke has one trusted caller; the hub token is
shared, so the spoke cannot otherwise tell callers apart).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_LEDGER_PATH = Path.home() / ".hermes-hub" / "spoke_ledger.db"
REQUEST_CHARS = 300
ANSWER_CHARS = 500
RECENT_WINDOW_S = 2 * 3600
RECENT_LIMIT = 5
RETENTION_S = 24 * 3600
DEFAULT_CALLER = "default"
#: Overrides the ledger location (live gates use a throwaway path).
ENV_LEDGER_PATH = "HERMES_HUB_SPOKE_LEDGER"


def caller_label(metadata: Optional[Dict[str, Any]]) -> str:
    value = (metadata or {}).get("callerName")
    return str(value).strip()[:64] if value and str(value).strip() else DEFAULT_CALLER


class RequestLedger:
    def __init__(
        self, db_path: Optional[Path] = None, *, clock: Callable[[], float] = time.time
    ) -> None:
        env_path = os.environ.get(ENV_LEDGER_PATH, "").strip()
        self.db_path = Path(db_path or env_path or DEFAULT_LEDGER_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                task_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                caller TEXT NOT NULL,
                request TEXT NOT NULL,
                state TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def record_start(self, *, task_id: str, context_id: str, caller: str, request: str) -> None:
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO requests VALUES (?, ?, ?, ?, 'working', '', ?, ?)",
                (task_id, context_id, caller, request[:REQUEST_CHARS], now, now),
            )
            self._conn.commit()

    def record_end(self, *, task_id: str, state: str, answer: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET state = ?, answer = ?, updated_at = ? WHERE task_id = ?",
                (state, answer[:ANSWER_CHARS], self._clock(), task_id),
            )
            self._conn.commit()

    def recent(
        self,
        caller: str,
        *,
        within_s: float = RECENT_WINDOW_S,
        limit: int = RECENT_LIMIT,
        exclude_task_id: str = "",
    ) -> List[Dict[str, Any]]:
        now = self._clock()
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, context_id, request, state, answer, started_at FROM requests "
                "WHERE caller = ? AND started_at >= ? AND task_id != ? "
                "ORDER BY started_at DESC LIMIT ?",
                (caller, now - within_s, exclude_task_id, limit),
            ).fetchall()
        return [
            {
                "task_id": r[0],
                "context_id": r[1],
                "request": r[2],
                "state": r[3],
                "answer": r[4],
                "age_s": int(now - r[5]),
            }
            for r in rows
        ]

    def prune(self) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM requests WHERE started_at < ?", (self._clock() - RETENTION_S,)
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])


def _age(seconds: int) -> str:
    return f"{seconds // 60} min ago" if seconds >= 60 else f"{seconds} s ago"


def format_recent_requests(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = ["Recent requests from this caller (last 2 h):"]
    for e in entries:
        request = " ".join(str(e.get("request") or "").split())
        if e.get("state") in ("working", "submitted"):
            outcome = f"still running (task {e['task_id']})"
        else:
            answer = " ".join(str(e.get("answer") or "").split())
            outcome = f"{e.get('state')} (task {e['task_id']}): {answer}"
        lines.append(f"- {_age(int(e.get('age_s') or 0))}: \"{request}\" -> {outcome}")
    lines.append(
        "If this request duplicates one of these, say so and return/refer to the "
        "earlier result instead of redoing the work, unless the caller explicitly "
        "asks to redo it."
    )
    return "\n".join(lines)
