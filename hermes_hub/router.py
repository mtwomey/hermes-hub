"""Routes an inbound A2A request to a named spoke's live WebSocket (H6, M3).

``Router`` owns the live connection map (spoke name -> an object able to
``send(frame)`` and register a per-task frame callback) and exposes
``route_task``, an async generator that yields every frame the spoke emits
for one task in arrival order — this is what lets the hub's SSE layer
forward frames incrementally instead of buffering until completion (Gate 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, AsyncIterator, Dict, Optional, Protocol

from . import artifacts
logger = logging.getLogger(__name__)

#: hermesError code for a task whose spoke WebSocket dropped mid-task.
SPOKE_DISCONNECTED = "spoke_disconnected"
TTL_EXPIRED = "ttl_expired"

from .protocol import (
    FRAME_ARTIFACT_BEGIN,
    FRAME_ARTIFACT_CHUNK,
    FRAME_ARTIFACT_END,
    build_artifact_begin_frame,
    build_artifact_chunk_frame,
    build_artifact_end_frame,
    build_task_artifact_frame,
    build_task_cancel_frame,
    build_task_failed_frame,
    build_task_frame,
    chunk_artifact_bytes,
    is_terminal_frame,
    reassemble_artifact_chunks,
)


class SpokeUnavailableError(Exception):
    """Raised when a request targets a spoke that is not currently connected.

    H10: the hub does not queue for an offline spoke; it fails fast.
    """


class TaskTTLExpired(TimeoutError):
    """Raised when a routed task reaches its hard TTL (Phase 1, D5) without a
    terminal frame. Distinct from a caller giving up: the hub never fails a
    task merely because nobody is waiting for it."""


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

    def __init__(self, *, base_url: str = "") -> None:
        self._connections: Dict[str, SpokeConnection] = {}
        # task_id -> asyncio.Queue of frames received from the spoke for
        # that task; populated by dispatch_frame_from_spoke, drained by
        # route_task's async generator.
        self._task_queues: Dict[str, "asyncio.Queue[Dict[str, Any]]"] = {}
        # task_id -> spoke name, so a spoke disconnect can fail its in-flight
        # tasks promptly instead of leaving them to the TTL.
        self._task_spokes: Dict[str, str] = {}
        #: Frames that arrived for a task_id with no live route (logged, Phase 1.2).
        self.late_frame_count = 0
        #: Base URL used to build download links for reassembled artifacts
        #: (Task 2.4). The hub serves these under
        #: ``artifacts.ARTIFACT_DOWNLOAD_PATH``.
        self.base_url = base_url.rstrip("/")

    def register_connection(self, spoke_name: str, connection: SpokeConnection) -> None:
        self._connections[spoke_name] = connection

    def unregister_connection(self, spoke_name: str) -> None:
        self._connections.pop(spoke_name, None)
        for task_id, owner in list(self._task_spokes.items()):
            if owner != spoke_name:
                continue
            queue = self._task_queues.get(task_id)
            if queue is None:
                continue
            frame = build_task_failed_frame(
                task_id=task_id,
                error=f"spoke '{spoke_name}' disconnected before the task finished",
            )
            frame["hermes_error"] = SPOKE_DISCONNECTED
            queue.put_nowait(frame)

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
            return
        # Phase 1.2: never drop silently. Log id + type only (no payload).
        self.late_frame_count += 1
        logger.warning(
            "late frame for unknown task_id=%s type=%s (no live route; dropped)",
            task_id,
            frame.get("type"),
        )

    async def cancel_task(self, task_id: str, reason: str = "cancelled") -> bool:
        """Phase 3.3: tell the spoke running ``task_id`` to stop.

        Best-effort: returns False when the task has no live route or its
        spoke is not connected (the caller still ends the task CANCELED)."""
        spoke_name = self._task_spokes.get(task_id)
        connection = self._connections.get(spoke_name) if spoke_name else None
        if connection is None:
            return False
        try:
            await connection.send(build_task_cancel_frame(task_id=task_id, reason=reason))
        except Exception:  # noqa: BLE001 - spoke link gone; cancel still applies hub-side
            logger.warning("task_cancel for task_id=%s could not be sent to %s", task_id, spoke_name)
            return False
        logger.info("sent task_cancel task_id=%s spoke=%s reason=%s", task_id, spoke_name, reason)
        return True

    async def route_task(
        self,
        *,
        spoke_name: str,
        task_id: str,
        context_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        credential: str = "",
        inbound_file: Optional[Dict[str, Any]] = None,
        ttl_seconds: float = 1800.0,
        timeout_seconds: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a task to ``spoke_name`` and yield every frame it emits, in
        arrival order, until a terminal frame (complete/failed) or the hard
        TTL (raises :class:`TaskTTLExpired`). ``timeout_seconds`` is a
        deprecated alias for ``ttl_seconds``.

        Raises :class:`SpokeUnavailableError` immediately (H10, no queueing)
        if the spoke is not currently connected.

        ``credential`` (V5): relayed opaquely and verbatim into the outbound
        task frame. The router never validates it, never compares it, and
        never stores it beyond the single ``send`` call below -- it must not
        appear in any router attribute, queue entry, or cache once this
        method returns (V5/V5a: the hub relays, only the spoke checks).
        """
        connection = self._connections.get(spoke_name)
        if connection is None:
            raise SpokeUnavailableError(f"spoke '{spoke_name}' is not currently connected")

        if timeout_seconds is not None:
            ttl_seconds = timeout_seconds
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._task_queues[task_id] = queue
        self._task_spokes[task_id] = spoke_name
        # In-flight artifact reassembly buffers, keyed by artifact_id.
        # Populated on artifact_begin, appended to on artifact_chunk,
        # flushed (verified + stored + synthesized into a task_artifact
        # frame) on artifact_end (Task 2.4).
        artifact_buffers: Dict[str, Dict[str, Any]] = {}
        try:
            if inbound_file is not None:
                # Task 2.5: relay a caller-supplied file to the spoke BEFORE
                # the task frame, so it's on disk before the agent runs.
                await self._send_inbound_file(connection, task_id=task_id, inbound_file=inbound_file)

            await connection.send(
                build_task_frame(
                    task_id=task_id,
                    context_id=context_id,
                    text=text,
                    metadata=metadata,
                    credential=credential,
                )
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + ttl_seconds
            ttl_message = f"task {task_id} on spoke {spoke_name} reached its {ttl_seconds:g}s TTL"
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self.cancel_task(task_id, reason=TTL_EXPIRED)
                    raise TaskTTLExpired(ttl_message)
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    # Phase 3.3: auto-stop -- the spoke is told to interrupt.
                    await self.cancel_task(task_id, reason=TTL_EXPIRED)
                    raise TaskTTLExpired(ttl_message) from None
                frame_type = frame.get("type")

                if frame_type == FRAME_ARTIFACT_BEGIN:
                    artifact_buffers[frame["artifact_id"]] = {"begin": frame, "chunks": []}
                    continue
                if frame_type == FRAME_ARTIFACT_CHUNK:
                    buf = artifact_buffers.get(frame.get("artifact_id"))
                    if buf is not None:
                        buf["chunks"].append(frame)
                    continue
                if frame_type == FRAME_ARTIFACT_END:
                    artifact_id = frame.get("artifact_id")
                    buf = artifact_buffers.pop(artifact_id, None)
                    if buf is None:
                        continue
                    begin = buf["begin"]
                    data = reassemble_artifact_chunks(buf["chunks"])
                    digest = hashlib.sha256(data).hexdigest()
                    declared_digest = str(begin.get("sha256") or "")
                    if declared_digest and digest != declared_digest:
                        failure = build_task_failed_frame(
                            task_id=task_id,
                            error=(
                                f"artifact {artifact_id} failed SHA-256 verification: "
                                f"expected {declared_digest}, got {digest}"
                            ),
                        )
                        yield failure
                        return
                    stored = artifacts.store_artifact_bytes(
                        task_id=task_id,
                        name=str(begin.get("name") or artifact_id),
                        data=data,
                        mime_type=str(begin.get("mime_type") or "application/octet-stream"),
                        artifact_id=artifact_id,
                    )
                    url = f"{self.base_url}{artifacts.ARTIFACT_DOWNLOAD_PATH}/{task_id}/{artifact_id}"
                    synthesized = build_task_artifact_frame(
                        task_id=task_id,
                        artifact_id=artifact_id,
                        name=stored.name,
                        mime_type=stored.mime_type,
                    )
                    synthesized["sha256"] = stored.sha256
                    synthesized["size_bytes"] = stored.size_bytes
                    synthesized["url"] = url
                    yield synthesized
                    continue

                if frame_type == "task_artifact" and frame.get("data"):
                    # A small artifact arrived inline rather than chunked.
                    # Store it hub-side and attach a download URL, so
                    # peer_fetch_artifact works identically for small and
                    # large files. Without this, the inline path returns
                    # sha256 metadata with no fetchable location and the
                    # download route 404s (W3 M1 regression).
                    import base64 as _base64

                    artifact_id = str(frame.get("artifact_id") or "artifact")
                    data = _base64.b64decode(frame["data"])
                    declared_digest = str(frame.get("sha256") or "")
                    digest = hashlib.sha256(data).hexdigest()
                    if declared_digest and digest != declared_digest:
                        yield build_task_failed_frame(
                            task_id=task_id,
                            error=(
                                f"artifact {artifact_id} failed SHA-256 verification: "
                                f"expected {declared_digest}, got {digest}"
                            ),
                        )
                        return
                    stored = artifacts.store_artifact_bytes(
                        task_id=task_id,
                        name=str(frame.get("name") or artifact_id),
                        data=data,
                        mime_type=str(frame.get("mime_type") or "application/octet-stream"),
                        artifact_id=artifact_id,
                    )
                    enriched = dict(frame)
                    enriched["sha256"] = stored.sha256
                    enriched["size_bytes"] = stored.size_bytes
                    enriched["url"] = (
                        f"{self.base_url}{artifacts.ARTIFACT_DOWNLOAD_PATH}"
                        f"/{task_id}/{artifact_id}"
                    )
                    yield enriched
                    continue

                yield frame
                if is_terminal_frame(frame):
                    return
        finally:
            self._task_queues.pop(task_id, None)
            self._task_spokes.pop(task_id, None)

    async def _send_inbound_file(
        self, connection: SpokeConnection, *, task_id: str, inbound_file: Dict[str, Any]
    ) -> None:
        """Task 2.5: relay a caller-supplied file to the spoke as a chunked
        artifact_begin/chunk*/end sequence, ahead of the task frame itself.

        ``inbound_file`` shape: ``{"name": str, "mime_type": str, "data": bytes}``.
        """
        data = inbound_file.get("data") or b""
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"inbound_{task_id}"
        await connection.send(
            build_artifact_begin_frame(
                task_id=task_id,
                artifact_id=artifact_id,
                name=str(inbound_file.get("name") or "upload.bin"),
                mime_type=str(inbound_file.get("mime_type") or "application/octet-stream"),
                total_bytes=len(data),
                sha256=digest,
            )
        )
        for seq, chunk in enumerate(chunk_artifact_bytes(data)):
            await connection.send(
                build_artifact_chunk_frame(task_id=task_id, artifact_id=artifact_id, seq=seq, data=chunk)
            )
        await connection.send(build_artifact_end_frame(task_id=task_id, artifact_id=artifact_id))
