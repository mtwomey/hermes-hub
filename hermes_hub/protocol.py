"""Wire frame shapes exchanged over the spoke<->hub WebSocket (H3).

Both sides speak small JSON dicts over one persistent connection. This
module is the single source of truth for frame shape so the hub and spoke
never drift silently.

Frames, spoke -> hub:
  register       (see spoke_client.build_registration_frame)
  task_status    heartbeat / non-terminal state update for a routed task
  task_artifact  a produced small text artifact for a routed task
  task_complete  final successful answer for a routed task
  task_failed    final failure for a routed task

Frames, hub -> spoke:
  task           a routed A2A request to execute locally

Frames, either direction (Task 2.2, W2 -- artifact movement is
bidirectional: spoke produces outbound files, caller sends inbound files):
  artifact_begin announces an incoming binary artifact (name, size, sha256)
  artifact_chunk one base64-encoded chunk of raw bytes, ordered by ``seq``
  artifact_end   the final chunk for this artifact has been sent
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Iterable, Iterator, List, Optional

FRAME_TASK = "task"
FRAME_TASK_STATUS = "task_status"
FRAME_TASK_ARTIFACT = "task_artifact"
FRAME_TASK_COMPLETE = "task_complete"
FRAME_TASK_FAILED = "task_failed"
FRAME_ARTIFACT_BEGIN = "artifact_begin"
FRAME_ARTIFACT_CHUNK = "artifact_chunk"
FRAME_ARTIFACT_END = "artifact_end"

TERMINAL_FRAME_TYPES = frozenset({FRAME_TASK_COMPLETE, FRAME_TASK_FAILED})

#: 256 KiB of raw bytes per chunk frame (base64-expanded on the wire).
CHUNK_BYTES = 262144

#: Matches hermes-peer's max_inline_bytes (Task 2.2, V14).
INLINE_MAX_BYTES = 65536


def build_task_frame(
    *,
    task_id: str,
    context_id: str,
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
    credential: str = "",
) -> Dict[str, Any]:
    """Hub -> spoke: a routed A2A request to execute locally.

    ``credential`` (V5/V5a): an opaque, caller-supplied per-spoke secret.
    This layer must not parse, validate, or assume any structure on it --
    it is relayed verbatim so V5b (signatures) can later become a drop-in
    replacement for what the caller puts in and what the spoke checks,
    with no change to this frame shape.
    """
    return {
        "type": FRAME_TASK,
        "task_id": task_id,
        "context_id": context_id,
        "text": text,
        "metadata": dict(metadata or {}),
        "credential": credential,
    }


def build_task_status_frame(*, task_id: str, state: str = "working") -> Dict[str, Any]:
    """Spoke -> hub: a non-terminal heartbeat for a routed task."""
    return {"type": FRAME_TASK_STATUS, "task_id": task_id, "state": state}


def build_task_artifact_frame(
    *,
    task_id: str,
    artifact_id: str,
    name: str,
    text: str = "",
    mime_type: str = "text/plain",
    data: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Spoke -> hub: an inline artifact produced for a routed task.

    Text-only usage (``text=``) is unchanged. Small binary artifacts under
    ``INLINE_MAX_BYTES`` may also be sent inline via ``data=`` (raw bytes) --
    base64-encoded on the wire with a SHA-256 for verification, mirroring
    hermes-peer's inline path (V14). Artifacts over the threshold use the
    chunked ``artifact_begin``/``artifact_chunk``/``artifact_end`` sequence
    instead (Task 2.2/2.3); this frame's own text-only shape from earlier
    milestones is preserved for backward compatibility.
    """
    frame: Dict[str, Any] = {
        "type": FRAME_TASK_ARTIFACT,
        "task_id": task_id,
        "artifact_id": artifact_id,
        "name": name,
        "text": text,
        "mime_type": mime_type,
    }
    if data is not None:
        import hashlib

        frame["data"] = base64.b64encode(data).decode("ascii")
        frame["sha256"] = hashlib.sha256(data).hexdigest()
    return frame


def build_artifact_begin_frame(
    *,
    task_id: str,
    artifact_id: str,
    name: str,
    mime_type: str,
    total_bytes: int,
    sha256: str,
) -> Dict[str, Any]:
    """Either direction: announces an incoming binary artifact before its
    chunks arrive. ``sha256`` is the hash of the *complete* payload,
    declared up front so the receiver can verify on reassembly and fail the
    task on mismatch (Task 2.4) rather than silently accepting corruption."""
    return {
        "type": FRAME_ARTIFACT_BEGIN,
        "task_id": task_id,
        "artifact_id": artifact_id,
        "name": name,
        "mime_type": mime_type,
        "total_bytes": total_bytes,
        "sha256": sha256,
    }


def build_artifact_chunk_frame(
    *, task_id: str, artifact_id: str, seq: int, data: bytes
) -> Dict[str, Any]:
    """Either direction: one chunk of raw bytes, base64-encoded for the JSON
    wire format. ``seq`` is used to reassemble in order regardless of
    arrival order (frames could theoretically race on an unordered
    transport; WebSocket text frames are ordered per-connection, but
    reassembly is defensive anyway)."""
    return {
        "type": FRAME_ARTIFACT_CHUNK,
        "task_id": task_id,
        "artifact_id": artifact_id,
        "seq": seq,
        "data": base64.b64encode(data).decode("ascii"),
    }


def build_artifact_end_frame(*, task_id: str, artifact_id: str) -> Dict[str, Any]:
    """Either direction: signals the last chunk for this artifact has been
    sent."""
    return {"type": FRAME_ARTIFACT_END, "task_id": task_id, "artifact_id": artifact_id}


def chunk_artifact_bytes(data: bytes, *, chunk_bytes: int = CHUNK_BYTES) -> Iterator[bytes]:
    """Split raw bytes into fixed-size chunks for ``artifact_chunk`` frames."""
    for offset in range(0, len(data), chunk_bytes):
        yield data[offset : offset + chunk_bytes]


def reassemble_artifact_chunks(chunk_frames: Iterable[Dict[str, Any]]) -> bytes:
    """Reassemble a sequence of ``artifact_chunk`` frames back into the
    original bytes, ordered by ``seq`` (not arrival order)."""
    ordered = sorted(chunk_frames, key=lambda f: f.get("seq", 0))
    return b"".join(base64.b64decode(f["data"]) for f in ordered)


def build_task_complete_frame(*, task_id: str, text: str) -> Dict[str, Any]:
    """Spoke -> hub: the final successful answer for a routed task."""
    return {"type": FRAME_TASK_COMPLETE, "task_id": task_id, "text": text}


def build_task_failed_frame(*, task_id: str, error: str) -> Dict[str, Any]:
    """Spoke -> hub: the final failure for a routed task."""
    return {"type": FRAME_TASK_FAILED, "task_id": task_id, "error": error}


def is_terminal_frame(frame: Dict[str, Any]) -> bool:
    return frame.get("type") in TERMINAL_FRAME_TYPES
