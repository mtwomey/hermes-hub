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
from typing import Any, Dict, Mapping

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from .router import Router, SpokeUnavailableError


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

    def __init__(self, *, router: Router, timeout_seconds: float = 120.0) -> None:
        self.router = router
        self.timeout_seconds = timeout_seconds

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = await open_task(context, event_queue)
        await updater.start_work()

        metadata = message_metadata(context)
        spoke_name = str(metadata.get("targetSpoke") or metadata.get("target_spoke") or "")
        credential = str(metadata.get("spokeCredential") or "")
        # Do not carry the credential twice: it travels in the frame's own
        # `credential` field. Leaving it duplicated in `metadata` as well
        # would widen the surface for accidental logging.
        metadata = {k: v for k, v in metadata.items() if k != "spokeCredential"}
        text = message_text(context)

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
                timeout_seconds=self.timeout_seconds,
            ):
                frame_type = frame.get("type")
                if frame_type == "task_status":
                    # Every incremental heartbeat becomes its own
                    # TaskStatusUpdateEvent, published as soon as it's
                    # dispatched -- not batched -- so SSE forwards it
                    # immediately (Gate 3).
                    await updater.update_status(TaskState.TASK_STATE_WORKING)
                elif frame_type == "task_artifact":
                    await updater.add_artifact(
                        [Part(text=frame.get("text", ""))],
                        artifact_id=str(frame.get("artifact_id") or "artifact"),
                        name=str(frame.get("name") or "artifact"),
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
                            metadata={"hermesError": "spoke_task_failed"},
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
        except TimeoutError as exc:
            await updater.failed(
                updater.new_agent_message(
                    [Part(text=str(exc))],
                    metadata={"hermesError": "timeout"},
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = await open_task(context, event_queue)
        await updater.cancel()
