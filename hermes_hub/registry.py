"""Registry of currently-connected spokes (in-memory for M1; SQLite later).

H5: the hub's aggregate AgentCard advertises the union of all connected
spokes' skills, each tagged with its owning spoke's name.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SpokeInfo:
    """A single registered spoke."""

    name: str
    skills: List[Dict[str, Any]] = field(default_factory=list)
    connected_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SpokeRegistry:
    """Tracks connected spokes by name.

    Thread-safe: the hub's WebSocket server and its HTTP/A2A surface run on
    different tasks/threads and both touch this registry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spokes: Dict[str, SpokeInfo] = {}

    def register(
        self,
        *,
        name: str,
        skills: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SpokeInfo:
        """Register (or re-register) a spoke as connected."""
        info = SpokeInfo(
            name=name,
            skills=list(skills or []),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._spokes[name] = info
        return info

    def deregister(self, name: str) -> None:
        """Remove a spoke from the connected set (e.g. on disconnect)."""
        with self._lock:
            self._spokes.pop(name, None)

    def list_connected(self) -> List[SpokeInfo]:
        """All currently-connected spokes, in registration order... actually name order for determinism."""
        with self._lock:
            return [self._spokes[name] for name in sorted(self._spokes)]

    def get(self, name: str) -> Optional[SpokeInfo]:
        with self._lock:
            return self._spokes.get(name)

    def is_connected(self, name: str) -> bool:
        with self._lock:
            return name in self._spokes
