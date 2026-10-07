"""Phase 2.1 (BEA-305, D4): transport dedup on the A2A ``messageId``.

A caller that retries a ``SendMessage`` with the same ``messageId`` (its own
transport retry, a proxy replay, a raw-curl double submit) must attach to the
task the first send created, never dispatch a second spoke execution.

Mechanics: :class:`MessageIdIndex` maps ``messageId -> Future[task_id]``.
The first send reserves the entry synchronously (no ``await`` between the
check and the insert, so concurrent duplicates on the one event loop cannot
both win); :class:`HubExecutor` binds the SDK-minted task id as soon as
``execute`` starts. Duplicates await that binding and are answered from the
task store. Entries are bounded (LRU) and expire after ``ttl_seconds``
(>= the task TTL, so a retry is recognised for the task's whole life).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, AsyncGenerator, Callable, Optional, Tuple

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import GetTaskRequest, SubscribeToTaskRequest

logger = logging.getLogger(__name__)

TERMINAL_STATE_NAMES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}
BIND_WAIT_SECONDS = 30.0


class MessageIdIndex:
    def __init__(
        self,
        *,
        max_entries: int = 4096,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: "OrderedDict[str, Tuple[asyncio.Future, float]]" = OrderedDict()

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, (_, at) in self._entries.items() if now - at > self.ttl_seconds]:
            self._entries.pop(key, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def reserve(self, message_id: str) -> Tuple[asyncio.Future, bool]:
        """Return ``(future, is_new)``. Synchronous by design (atomic on the loop)."""
        self._prune()
        existing = self._entries.get(message_id)
        if existing is not None:
            return existing[0], False
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._entries[message_id] = (fut, self._clock())
        return fut, True

    def bind(self, message_id: str, task_id: str) -> None:
        entry = self._entries.get(message_id)
        if entry is not None and not entry[0].done():
            entry[0].set_result(task_id)

    def release(self, message_id: str, exc: Optional[BaseException] = None) -> None:
        """First send failed before a task existed: forget it so a retry can run."""
        entry = self._entries.pop(message_id, None)
        if entry is not None and not entry[0].done():
            entry[0].set_exception(exc or RuntimeError("original send failed"))
            entry[0].exception()  # mark retrieved; waiters re-raise their own copy

    def __len__(self) -> int:
        return len(self._entries)


def _message_id(params: Any) -> str:
    message = getattr(params, "message", None)
    if message is None or getattr(message, "task_id", ""):
        # A message continuing an explicit task is not a fresh dispatch.
        return ""
    return str(getattr(message, "message_id", "") or "")


class DedupRequestHandler(DefaultRequestHandler):
    def __init__(self, *args: Any, message_index: MessageIdIndex, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.message_index = message_index

    async def _existing_task_id(self, fut: asyncio.Future) -> str:
        return await asyncio.wait_for(asyncio.shield(fut), BIND_WAIT_SECONDS)

    async def _stored_task(self, task_id: str, context: Any) -> Any:
        """The executor binds the id before the SDK persists the first Task
        event, so a racing duplicate may briefly see TASK_NOT_FOUND."""
        from a2a.utils.errors import TaskNotFoundError

        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            try:
                task = await self.on_get_task(GetTaskRequest(id=task_id), context)
            except TaskNotFoundError:
                task = None
            if task is not None or asyncio.get_running_loop().time() >= deadline:
                return task
            await asyncio.sleep(0.05)

    async def on_message_send(self, params, context):  # type: ignore[override]
        mid = _message_id(params)
        if not mid:
            return await super().on_message_send(params, context)
        fut, is_new = self.message_index.reserve(mid)
        if is_new:
            try:
                return await super().on_message_send(params, context)
            except BaseException as exc:
                if not fut.done():
                    self.message_index.release(mid, exc if isinstance(exc, Exception) else None)
                raise
        task_id = await self._existing_task_id(fut)
        logger.info("duplicate messageId attached to existing task_id=%s", task_id)
        return_immediately = bool(getattr(params.configuration, "return_immediately", False))
        while True:
            task = await self._stored_task(task_id, context)
            if task is None or return_immediately:
                return task
            from a2a.types import TaskState

            if TaskState.Name(task.status.state) in TERMINAL_STATE_NAMES:
                return task
            await asyncio.sleep(0.5)

    async def on_message_send_stream(self, params, context) -> AsyncGenerator[Any, None]:  # type: ignore[override]
        mid = _message_id(params)
        if not mid:
            async for event in super().on_message_send_stream(params, context):
                yield event
            return
        fut, is_new = self.message_index.reserve(mid)
        if is_new:
            try:
                async for event in super().on_message_send_stream(params, context):
                    yield event
            except BaseException as exc:
                if not fut.done():
                    self.message_index.release(mid, exc if isinstance(exc, Exception) else None)
                raise
            return
        task_id = await self._existing_task_id(fut)
        logger.info("duplicate messageId (stream) attached to existing task_id=%s", task_id)
        task = await self._stored_task(task_id, context)
        if task is not None:
            yield task
            from a2a.types import TaskState

            if TaskState.Name(task.status.state) in TERMINAL_STATE_NAMES:
                return
        async for event in self.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), context):
            yield event
