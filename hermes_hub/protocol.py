"""Wire frame shapes exchanged over the spoke<->hub WebSocket (H3).

Both sides speak small JSON dicts over one persistent connection. This
module is the single source of truth for frame shape so the hub and spoke
never drift silently.

Frames, spoke -> hub:
  register       (see spoke_client.build_registration_frame)
  task_status    heartbeat / non-terminal state update for a routed task
  task_artifact  a produced file artifact for a routed task
  task_complete  final successful answer for a routed task
  task_failed    final failure for a routed task

Frames, hub -> spoke:
  task           a routed A2A request to execute locally
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

FRAME_TASK = "task"
FRAME_TASK_STATUS = "task_status"
FRAME_TASK_ARTIFACT = "task_artifact"
FRAME_TASK_COMPLETE = "task_complete"
FRAME_TASK_FAILED = "task_failed"

TERMINAL_FRAME_TYPES = frozenset({FRAME_TASK_COMPLETE, FRAME_TASK_FAILED})


def build_task_frame(
    *,
    task_id: str,
    context_id: str,
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Hub -> spoke: a routed A2A request to execute locally."""
    return {
        "type": FRAME_TASK,
        "task_id": task_id,
        "context_id": context_id,
        "text": text,
        "metadata": dict(metadata or {}),
    }


def build_task_status_frame(*, task_id: str, state: str = "working") -> Dict[str, Any]:
    """Spoke -> hub: a non-terminal heartbeat for a routed task."""
    return {"type": FRAME_TASK_STATUS, "task_id": task_id, "state": state}


def build_task_artifact_frame(
    *, task_id: str, artifact_id: str, name: str, text: str = "", mime_type: str = "text/plain"
) -> Dict[str, Any]:
    """Spoke -> hub: an inline text artifact produced for a routed task.

    v1 keeps artifacts inline-text-only over the WebSocket frame protocol;
    large-file/binary artifact routing through the hub is not built in this
    plan (deferred — see completion matrix deviation notes).
    """
    return {
        "type": FRAME_TASK_ARTIFACT,
        "task_id": task_id,
        "artifact_id": artifact_id,
        "name": name,
        "text": text,
        "mime_type": mime_type,
    }


def build_task_complete_frame(*, task_id: str, text: str) -> Dict[str, Any]:
    """Spoke -> hub: the final successful answer for a routed task."""
    return {"type": FRAME_TASK_COMPLETE, "task_id": task_id, "text": text}


def build_task_failed_frame(*, task_id: str, error: str) -> Dict[str, Any]:
    """Spoke -> hub: the final failure for a routed task."""
    return {"type": FRAME_TASK_FAILED, "task_id": task_id, "error": error}


def is_terminal_frame(frame: Dict[str, Any]) -> bool:
    return frame.get("type") in TERMINAL_FRAME_TYPES
