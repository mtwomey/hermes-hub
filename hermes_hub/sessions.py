"""``contextId`` -> Hermes session id mapping, spoke-side (H7/H8, mirrors
hermes-peer's D10 pattern as fresh code, not an import).

A2A's ``contextId`` groups related tasks into one logical conversation. This
maps onto a *reusable* Hermes session id, persisted in a small local SQLite
db, so a hub-routed conversation carries state across turns the same way
hermes-peer's direct-mesh sessions do (M5's whole point).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Optional

DEFAULT_DB_PATH = Path.home() / ".hermes-hub" / "spoke_sessions.db"
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_SIZE = 1000


class SessionStore:
    """SQLite-backed contextId -> session_id persistence, survives restarts."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_sessions (
                context_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def get(self, context_id: str) -> Optional[tuple[str, float]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, created_at FROM context_sessions WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def set(self, context_id: str, session_id: str, created_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO context_sessions (context_id, session_id, created_at) "
                "VALUES (?, ?, ?)",
                (context_id, session_id, created_at),
            )
            self._conn.commit()

    def delete(self, context_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM context_sessions WHERE context_id = ?", (context_id,))
            self._conn.commit()


class SessionMap:
    """Maps an A2A ``contextId`` to a stable Hermes ``session_id``.

    In-process cache backed by :class:`SessionStore` for persistence across
    process restarts. Entries expire after ``ttl_seconds``; the map is
    capped at ``max_size`` with oldest-first eviction.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_size: int = DEFAULT_MAX_SIZE,
        store: Optional[SessionStore] = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._store = store if store is not None else SessionStore()
        self._lock = threading.Lock()
        self._cache: Dict[str, tuple[str, float]] = {}
        self._mint_counter = 0

    def session_for(self, context_id: str) -> str:
        context_id = context_id or "no-context"
        now = time.time()
        with self._lock:
            cached = self._cache.get(context_id)
            if cached is not None:
                session_id, created_at = cached
                if now - created_at < self.ttl_seconds:
                    return session_id

            persisted = self._store.get(context_id)
            if persisted is not None:
                session_id, created_at = persisted
                if now - created_at < self.ttl_seconds:
                    self._cache[context_id] = (session_id, created_at)
                    return session_id

            session_id = f"hub-ctx-{context_id}-{int(now * 1_000_000)}-{self._mint_counter}"
            self._mint_counter += 1
            self._store.set(context_id, session_id, now)
            self._evict_if_full(context_id)
            self._cache[context_id] = (session_id, now)
            return session_id

    def _evict_if_full(self, keep: str) -> None:
        if len(self._cache) < self.max_size:
            return
        oldest_key = min(
            (k for k in self._cache if k != keep),
            key=lambda k: self._cache[k][1],
            default=None,
        )
        if oldest_key is not None:
            del self._cache[oldest_key]
            self._store.delete(oldest_key)
