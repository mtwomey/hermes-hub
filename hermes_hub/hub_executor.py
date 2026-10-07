"""The hub's AgentExecutor: bridges an inbound A2A task to the router (M3, M4).

Per H6, callers address a specific spoke by name. The target spoke name
travels in the inbound message's metadata under ``targetSpoke`` (the CLI in
M6 sets this from ``hermes-hub ask <spoke> "..."``). This executor pulls
that name out, calls :meth:`Router.route_task`, and republishes every frame
the spoke emits as the matching A2A ``TaskStatusUpdateEvent``/artifact/
completion event -- one at a time, as it arrives, which is what makes SSE to
the external caller genuinely incremental (Gate 3).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from .caller_contract import META_SPOKE_CREDENTIAL, META_TARGET_SPOKE
from .router import Router, SpokeUnavailableError, TaskTTLExpired


def message_text(context: RequestContext) -> str:
    try:
        text = context.get_user_input()
        if text:
            return str(text)
    except Exception:
        pass
    message = getattr(context, "message", None)
    if message is None:
        return ""
    return "\n".join(part.text for part in message.parts if part.text).strip()


def message_metadata(context: RequestContext) -> Dict[str, Any]:
    message = getattr(context, "message", None)
    metadata = getattr(message, "metadata", None) if message is not None else None
    if metadata is None:
        return {}
    try:
        from google.protobuf.json_format import MessageToDict

        return MessageToDict(metadata)
    except Exception:
        try:
            return dict(metadata)
        except Exception:
            return {}


def message_inbound_file(context: RequestContext) -> Dict[str, Any] | None:
    """Task 2.5: extract a caller-supplied file from the inbound message's
    parts, if present. Per the SDK's ``Part`` shape, a file part carries raw
    bytes in ``.raw`` plus ``.filename``/``.media_type``; a plain text part
    has none of these. Returns ``None`` when there is no file part."""
    message = getattr(context, "message", None)
    if message is None:
        return None
    for part in getattr(message, "parts", []) or []:
        raw = getattr(part, "raw", b"")
        if raw:
            return {
                "name": str(getattr(part, "filename", "") or "upload.bin"),
                "mime_type": str(getattr(part, "media_type", "") or "application/octet-stream"),
                "data": bytes(raw),
            }
    return None


async def open_task(context: RequestContext, event_queue: EventQueue) -> TaskUpdater:
    """Enqueue the initial Task before any status update (SDK requirement)."""
    if context.current_task is None:
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
    return TaskUpdater(event_queue, context.task_id, context.context_id)


class HubExecutor(AgentExecutor):
    """Routes an inbound A2A task to the named spoke via :class:`Router`."""

    def __init__(
        self,
        *,
        router: Router,
        ttl_seconds: float = 1800.0,
        timeout_seconds: float | None = None,
        now: Callable[[], datetime] | None = None,
        message_index: Any = None,
    ) -> None:
        self.router = router
        #: Phase 2.1: ``MessageIdIndex`` shared with ``DedupRequestHandler``.
        self.message_index = message_index
        self._now = now or (lambda: datetime.now(timezone.utc))
        # ``timeout_seconds`` is the deprecated pre-Phase-1 name.
        self.ttl_seconds = float(timeout_seconds if timeout_seconds is not None else ttl_seconds)

    @property
    def timeout_seconds(self) -> float:
        return self.ttl_seconds

    def _stamp(self) -> str:
        return self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if self.message_index is not None:
            message_id = str(getattr(getattr(context, "message", None), "message_id", "") or "")
            if message_id:
                self.message_index.bind(message_id, context.task_id)
        updater = await open_task(context, event_queue)
        await updater.start_work()
        # Phase 1.3: liveness metadata, merged into task.metadata by the SDK's
        # TaskManager so GetTask / peer_status can show elapsed + last-heard.
        liveness: Dict[str, str] = {"startedAt": self._stamp()}
        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=dict(liveness))

        metadata = message_metadata(context)
        spoke_name = str(metadata.get(META_TARGET_SPOKE) or metadata.get("target_spoke") or "")
        credential = str(metadata.get(META_SPOKE_CREDENTIAL) or "")
        # Do not carry the credential twice: it travels in the frame's own
        # `credential` field. Leaving it duplicated in `metadata` as well
        # would widen the surface for accidental logging.
        metadata = {k: v for k, v in metadata.items() if k != META_SPOKE_CREDENTIAL}
        text = message_text(context)
        inbound_file = message_inbound_file(context)

        if not spoke_name:
            await updater.failed(
                updater.new_agent_message(
                    [Part(text="No targetSpoke specified in message metadata.")],
                    metadata={"hermesError": "missing_target_spoke"},
                )
            )
            return

        try:
            async for frame in self.router.route_task(
                spoke_name=spoke_name,
                task_id=context.task_id,
                context_id=context.context_id,
                text=text,
                metadata=metadata,
                credential=credential,
                inbound_file=inbound_file,
                ttl_seconds=self.ttl_seconds,
            ):
                frame_type = frame.get("type")
                if frame_type == "task_status":
                    # Every incremental heartbeat becomes its own
                    # TaskStatusUpdateEvent, published as soon as it's
                    # dispatched -- not batched -- so SSE forwards it
                    # immediately (Gate 3).
                    liveness["lastHeartbeatAt"] = self._stamp()
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING, metadata=dict(liveness)
                    )
                elif frame_type == "task_artifact":
                    parts = [Part(text=str(frame.get("text") or ""))]
                    if frame.get("data"):
                        import base64

                        parts.append(
                            Part(
                                raw=base64.b64decode(frame["data"]),
                                filename=str(frame.get("name") or "artifact"),
                                media_type=str(frame.get("mime_type") or "application/octet-stream"),
                            )
                        )
                    artifact_metadata: Dict[str, Any] = {}
                    if frame.get("url"):
                        artifact_metadata["url"] = frame["url"]
                    if frame.get("sha256"):
                        artifact_metadata["sha256"] = frame["sha256"]
                    if frame.get("size_bytes") is not None:
                        artifact_metadata["size_bytes"] = frame["size_bytes"]
                    await updater.add_artifact(
                        parts,
                        artifact_id=str(frame.get("artifact_id") or "artifact"),
                        name=str(frame.get("name") or "artifact"),
                        metadata=artifact_metadata or None,
                    )
                elif frame_type == "task_complete":
                    await updater.complete(
                        updater.new_agent_message([Part(text=str(frame.get("text", "")))])
                    )
                    return
                elif frame_type == "task_failed":
                    await updater.failed(
                        updater.new_agent_message(
                            [Part(text=str(frame.get("error", "task failed")))],
                            metadata={
                                "hermesError": str(frame.get("hermes_error") or "spoke_task_failed")
                            },
                        )
                    )
                    return
        except SpokeUnavailableError as exc:
            await updater.failed(
                updater.new_agent_message(
                    [Part(text=str(exc))],
                    metadata={"hermesError": "spoke_unavailable"},
                )
            )
        except TaskTTLExpired as exc:
            await updater.failed(
                updater.new_agent_message(
                    [Part(text=str(exc))],
                    metadata={"hermesError": "ttl_expired"},
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = await open_task(context, event_queue)
        await updater.cancel()
