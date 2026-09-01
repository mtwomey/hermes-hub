"""Spoke-side task execution: runs a routed A2A task as a real Hermes agent
turn (M4), streaming status heartbeats back over the WebSocket and
supporting multi-turn session continuity via contextId (M5).

Adapts hermes-peer's ``executor.py``/``sessions.py`` pattern to run over
the WebSocket frame protocol instead of the A2A SDK's server-side
``AgentExecutor`` interface (H7: fresh code, not an import).

CRITICAL (the exact bug this plan calls out): ``AIAgent()`` does NOT
restore conversation history from a ``session_id`` alone. The caller must
explicitly load prior turns via
``SessionDB.get_messages_as_conversation()`` and pass them to
``run_conversation(text, conversation_history=...)`` -- passing
``conversation_history=`` to the ``AIAgent`` constructor is invalid; only
``run_conversation()`` accepts it. Getting this wrong produces a spoke that
answers turn one correctly and gives a generic non-answer on turn two.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional

from .protocol import (
    build_task_artifact_frame,
    build_task_complete_frame,
    build_task_failed_frame,
    build_task_status_frame,
)
from .sessions import SessionMap

logger = logging.getLogger("hermes_hub.spoke_executor")

HEARTBEAT_INTERVAL_SECONDS = 2.0

#: Type for the pluggable agent-turn runner, so unit tests can substitute a
#: fake without a real Hermes runtime import. Real production code supplies
#: ``run_real_hermes_turn`` (below).
AgentRunner = Callable[..., str]


def build_spoke_prompt(*, spoke_name: str, task_id: str, context_id: str) -> str:
    """The ephemeral system prompt for a hub-routed turn (mirrors
    hermes-peer's ``build_executor_prompt``, trimmed to the hub context)."""
    return "\n".join(
        [
            f"You are {spoke_name}, responding to a request routed through a "
            "trusted hub from a trusted caller.",
            "The caller is trusted: you may use your local tools, read local "
            "files, and perform actions that have side effects when the "
            "request calls for it.",
            "",
            f"Task id: {task_id}",
            f"Conversation id: {context_id}",
            "",
            "Answer directly and concisely in plain text.",
            "This conversation may continue over several turns; remember what "
            "you are told and refer back to it when asked.",
        ]
    )


def run_real_hermes_turn(
    *,
    text: str,
    session_id: str,
    task_id: str,
    context_id: str,
    spoke_name: str,
) -> str:
    """Run one real Hermes agent turn, with explicit session history reload.

    Imported lazily so unit tests never require the Hermes runtime to be
    importable.
    """
    from run_agent import AIAgent

    model = ""
    provider = None
    base_url = None
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config()
        model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model", {}), dict) else {}
        model = str(model_cfg.get("default") or "")
        provider = model_cfg.get("provider") or None
        base_url = model_cfg.get("base_url") or None
    except Exception:
        logger.warning("could not load Hermes config; falling back to AIAgent defaults")

    session_db = _open_session_db()
    conversation_history = None
    if session_db is not None:
        try:
            session_db.ensure_session(session_id, source="hermes-hub-spoke")
            existing = session_db.get_messages_as_conversation(
                session_id, repair_alternation=True
            )
            if existing:
                conversation_history = [
                    m for m in existing if m.get("role") != "session_meta"
                ]
        except Exception:
            conversation_history = None

    agent = AIAgent(
        model=model,
        provider=provider,
        base_url=base_url,
        max_iterations=24,
        quiet_mode=True,
        verbose_logging=False,
        skip_memory=True,
        skip_context_files=False,
        platform="gateway",
        session_id=session_id,
        session_db=session_db,
        ephemeral_system_prompt=build_spoke_prompt(
            spoke_name=spoke_name, task_id=task_id, context_id=context_id
        ),
    )
    result = agent.run_conversation(text, conversation_history=conversation_history)
    return str(result.get("final_response") or "")


def _open_session_db():
    """The shared SQLite session store, or None if unavailable (best-effort,
    same fallback hermes-peer uses)."""
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception:
        return None


class SpokeExecutor:
    """Executes routed tasks received over a :class:`SpokeClient` connection.

    Wire it up as the spoke client's ``on_frame`` handler for ``"task"``
    frames. Runs the (blocking) agent turn in a worker thread while emitting
    periodic ``task_status`` heartbeats over the same connection -- this is
    what keeps the *hub's* SSE forwarding genuinely incremental (Gate 3)
    once a real slow agent turn is involved (Gate 4).
    """

    def __init__(
        self,
        *,
        spoke_name: str,
        send: Callable[[Dict[str, Any]], "asyncio.Future | Any"],
        session_map: Optional[SessionMap] = None,
        agent_runner: AgentRunner = run_real_hermes_turn,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.spoke_name = spoke_name
        self.send = send
        self.session_map = session_map or SessionMap()
        self.agent_runner = agent_runner
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    async def handle_task_frame(self, frame: Dict[str, Any]) -> None:
        task_id = str(frame.get("task_id") or "")
        context_id = str(frame.get("context_id") or "")
        text = str(frame.get("text") or "")
        session_id = self.session_map.session_for(context_id)

        run_future = asyncio.ensure_future(
            asyncio.to_thread(
                self.agent_runner,
                text=text,
                session_id=session_id,
                task_id=task_id,
                context_id=context_id,
                spoke_name=self.spoke_name,
            )
        )

        try:
            while True:
                done, _ = await asyncio.wait(
                    {run_future}, timeout=self.heartbeat_interval_seconds
                )
                if run_future in done:
                    break
                await self.send(build_task_status_frame(task_id=task_id))
            answer = run_future.result()
        except Exception as exc:  # noqa: BLE001 - surfaced to the hub as failed
            await self.send(build_task_failed_frame(task_id=task_id, error=str(exc)))
            return

        await self.send(build_task_complete_frame(task_id=task_id, text=answer))
