"""Routes an inbound A2A request to a named spoke's live WebSocket (H6, M3).

``Router`` owns the live connection map (spoke name -> an object able to
``send(frame)`` and register a per-task frame callback) and exposes
``route_task``, an async generator that yields every frame the spoke emits
for one task in arrival order — this is what lets the hub's SSE layer
forward frames incrementally instead of buffering until completion (Gate 3).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Optional, Protocol

from .protocol import build_task_frame, is_terminal_frame


class SpokeUnavailableError(Exception):
    """Raised when a request targets a spoke that is not currently connected.

    H10: the hub does not queue for an offline spoke; it fails fast.
    """


class SpokeConnection(Protocol):
    """What the router needs from a live spoke connection."""

    async def send(self, frame: Dict[str, Any]) -> None: ...


class Router:
    """Routes tasks addressed by spoke name to that spoke's live connection.

    ``connections`` maps spoke name -> a live object implementing
    :class:`SpokeConnection`. The hub server registers/unregisters entries
    here as spokes connect/disconnect (kept separate from ``SpokeRegistry``,
    which tracks *declared skills*, because the router only needs "can I
    reach it right now").
    """

    def __init__(self) -> None:
        self._connections: Dict[str, SpokeConnection] = {}
        # task_id -> asyncio.Queue of frames received from the spoke for
        # that task; populated by dispatch_frame_from_spoke, drained by
        # route_task's async generator.
        self._task_queues: Dict[str, "asyncio.Queue[Dict[str, Any]]"] = {}

    def register_connection(self, spoke_name: str, connection: SpokeConnection) -> None:
        self._connections[spoke_name] = connection

    def unregister_connection(self, spoke_name: str) -> None:
        self._connections.pop(spoke_name, None)

    def is_available(self, spoke_name: str) -> bool:
        return spoke_name in self._connections

    async def dispatch_frame_from_spoke(self, frame: Dict[str, Any]) -> None:
        """Called by the hub server's per-spoke receive loop for every frame
        that isn't a registration frame; routes it to the right task's queue
        by ``task_id``."""
        task_id = frame.get("task_id")
        if not task_id:
            return
        queue = self._task_queues.get(task_id)
        if queue is not None:
            await queue.put(frame)

    async def route_task(
        self,
        *,
        spoke_name: str,
        task_id: str,
        context_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 120.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a task to ``spoke_name`` and yield every frame it emits, in
        arrival order, until a terminal frame (complete/failed) or timeout.

        Raises :class:`SpokeUnavailableError` immediately (H10, no queueing)
        if the spoke is not currently connected.
        """
        connection = self._connections.get(spoke_name)
        if connection is None:
            raise SpokeUnavailableError(f"spoke '{spoke_name}' is not currently connected")

        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._task_queues[task_id] = queue
        try:
            await connection.send(
                build_task_frame(task_id=task_id, context_id=context_id, text=text, metadata=metadata)
            )
            deadline = asyncio.get_event_loop().time() + timeout_seconds
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"task {task_id} on spoke {spoke_name} timed out")
                frame = await asyncio.wait_for(queue.get(), timeout=remaining)
                yield frame
                if is_terminal_frame(frame):
                    return
        finally:
            self._task_queues.pop(task_id, None)
