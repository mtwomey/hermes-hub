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
from typing import Any, AsyncIterator, Dict, Optional, Protocol

from . import artifacts
from .protocol import (
    FRAME_ARTIFACT_BEGIN,
    FRAME_ARTIFACT_CHUNK,
    FRAME_ARTIFACT_END,
    build_artifact_begin_frame,
    build_artifact_chunk_frame,
    build_artifact_end_frame,
    build_task_artifact_frame,
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
        #: Base URL used to build download links for reassembled artifacts
        #: (Task 2.4). The hub serves these under
        #: ``artifacts.ARTIFACT_DOWNLOAD_PATH``.
        self.base_url = base_url.rstrip("/")

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
        credential: str = "",
        inbound_file: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 300.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a task to ``spoke_name`` and yield every frame it emits, in
        arrival order, until a terminal frame (complete/failed) or timeout.

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

        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._task_queues[task_id] = queue
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
            deadline = asyncio.get_event_loop().time() + timeout_seconds
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"task {task_id} on spoke {spoke_name} timed out")
                frame = await asyncio.wait_for(queue.get(), timeout=remaining)
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
