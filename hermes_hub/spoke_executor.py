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
import hmac
import inspect
import logging
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .protocol import (
    INLINE_MAX_BYTES,
    build_artifact_begin_frame,
    build_artifact_chunk_frame,
    build_artifact_end_frame,
    build_task_artifact_frame,
    build_task_complete_frame,
    build_task_failed_frame,
    build_task_status_frame,
    chunk_artifact_bytes,
    reassemble_artifact_chunks,
)
from .sessions import SessionMap

logger = logging.getLogger("hermes_hub.spoke_executor")

HEARTBEAT_INTERVAL_SECONDS = 2.0

#: Default root under which each task gets its own scratch output
#: directory (Task 2.3). A real deployment may override this via
#: SpokeExecutor's ``artifact_root`` constructor argument.
DEFAULT_ARTIFACT_ROOT = Path.home() / ".hermes-hub" / "spoke-task-output"

#: Type for the pluggable agent-turn runner, so unit tests can substitute a
#: fake without a real Hermes runtime import. Real production code supplies
#: ``run_real_hermes_turn`` (below).
AgentRunner = Callable[..., str]


def build_spoke_prompt(
    *,
    spoke_name: str,
    task_id: str,
    context_id: str,
    output_dir: Optional[Path] = None,
    input_files: Optional[List[Path]] = None,
) -> str:
    """The ephemeral system prompt for a hub-routed turn (mirrors
    hermes-peer's ``build_executor_prompt``, trimmed to the hub context)."""
    lines = [
        f"You are {spoke_name}, responding to a request routed through a "
        "trusted hub from a trusted caller.",
        "The caller is trusted: you may use your local tools, read local "
        "files, and perform actions that have side effects when the "
        "request calls for it.",
        "",
        f"Task id: {task_id}",
        f"Conversation id: {context_id}",
    ]
    if output_dir is not None:
        lines += [
            "",
            f"If this task asks you to produce a file, write it into: {output_dir}",
            "Any file present in that directory when you finish will be sent back "
            "to the caller as an artifact automatically.",
        ]
    if input_files:
        lines += [
            "",
            "The caller attached the following file(s), already on disk:",
        ] + [f"  {p}" for p in input_files]
    lines += [
        "",
        "Answer directly and concisely in plain text.",
        "This conversation may continue over several turns; remember what "
        "you are told and refer back to it when asked.",
    ]
    return "\n".join(lines)


def run_real_hermes_turn(
    *,
    text: str,
    session_id: str,
    task_id: str,
    context_id: str,
    spoke_name: str,
    output_dir: Optional[Path] = None,
    input_files: Optional[List[Path]] = None,
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
            spoke_name=spoke_name,
            task_id=task_id,
            context_id=context_id,
            output_dir=output_dir,
            input_files=input_files,
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
        expected_credential: str = "",
        artifact_root: Optional[Path] = None,
        chunk_bytes: int = 262144,
    ) -> None:
        self.spoke_name = spoke_name
        self.send = send
        self.session_map = session_map or SessionMap()
        self.agent_runner = agent_runner
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        #: (Task 1.4, V5a) This spoke's own locally-resolved secret. An
        #: inbound task's credential is compared against this BEFORE any
        #: agent work begins. Empty means dev mode (allow) -- mirrors
        #: hermes-peer D5 / hub_server._check_token.
        self.expected_credential = expected_credential
        #: (Task 2.3, W2) Root directory under which each task gets its own
        #: scratch output directory, scanned for produced files after the
        #: agent turn.
        self.artifact_root = Path(artifact_root) if artifact_root is not None else DEFAULT_ARTIFACT_ROOT
        self.chunk_bytes = chunk_bytes
        #: Whether ``agent_runner`` accepts an ``output_dir`` kwarg. Older
        #: fake runners in existing tests do not; call them without it so
        #: this addition stays backward-compatible.
        try:
            sig_params = inspect.signature(agent_runner).parameters
            self._agent_runner_accepts_output_dir = "output_dir" in sig_params
            self._agent_runner_accepts_input_files = "input_files" in sig_params
        except (TypeError, ValueError):
            self._agent_runner_accepts_output_dir = False
            self._agent_runner_accepts_input_files = False
        #: (Task 2.5, W2) In-flight inbound-artifact reassembly buffers,
        #: keyed by task_id -> artifact_id -> {"begin": frame, "chunks": []}.
        #: Populated by handle_frame on artifact_begin/chunk/end received
        #: BEFORE the matching task frame.
        self._inbound_artifact_buffers: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def handle_frame(self, frame: Dict[str, Any]) -> None:
        """General dispatcher for every frame the spoke client receives.

        Task 2.5: artifact_begin/artifact_chunk/artifact_end frames for an
        inbound (caller -> spoke) file arrive BEFORE the matching task
        frame and are reassembled here; the ``task`` frame itself is
        forwarded to :meth:`handle_task_frame` together with any files that
        were reassembled for its ``task_id``.
        """
        frame_type = frame.get("type")
        if frame_type == "artifact_begin":
            task_id = str(frame.get("task_id") or "")
            artifact_id = str(frame.get("artifact_id") or "")
            self._inbound_artifact_buffers.setdefault(task_id, {})[artifact_id] = {
                "begin": frame,
                "chunks": [],
            }
            return
        if frame_type == "artifact_chunk":
            task_id = str(frame.get("task_id") or "")
            artifact_id = str(frame.get("artifact_id") or "")
            buf = self._inbound_artifact_buffers.get(task_id, {}).get(artifact_id)
            if buf is not None:
                buf["chunks"].append(frame)
            return
        if frame_type == "artifact_end":
            # Reassembly into a file happens lazily in handle_task_frame,
            # once the task_id's input directory is known; nothing to do
            # here beyond leaving the buffer populated for that lookup.
            return
        if frame_type == "task":
            await self.handle_task_frame(frame)
            return

    async def handle_task_frame(self, frame: Dict[str, Any]) -> None:
        task_id = str(frame.get("task_id") or "")
        context_id = str(frame.get("context_id") or "")
        text = str(frame.get("text") or "")
        presented_credential = str(frame.get("credential") or "")

        if self.expected_credential and not hmac.compare_digest(
            presented_credential, self.expected_credential
        ):
            # Rejection must be indistinguishable in shape from any other
            # failure to an observer -- a generic task_failed frame. Never
            # echo the presented credential back in the error (Task 1.4).
            logger.info(
                "task %s rejected: credential check failed before agent invocation",
                task_id,
            )
            await self.send(
                build_task_failed_frame(task_id=task_id, error="task rejected")
            )
            return

        session_id = self.session_map.session_for(context_id)
        logger.info("task %s: credential accepted, invoking agent", task_id)

        output_dir = self.artifact_root / task_id
        output_dir.mkdir(parents=True, exist_ok=True)

        input_files = self._reassemble_inbound_files(task_id=task_id, output_dir=output_dir)
        for f in input_files:
            logger.info(
                "task %s: reassembled inbound file %s (%d bytes)", task_id, f, f.stat().st_size
            )

        runner_kwargs: Dict[str, Any] = dict(
            text=text,
            session_id=session_id,
            task_id=task_id,
            context_id=context_id,
            spoke_name=self.spoke_name,
        )
        if self._agent_runner_accepts_output_dir:
            runner_kwargs["output_dir"] = output_dir
        if self._agent_runner_accepts_input_files:
            runner_kwargs["input_files"] = input_files

        run_future = asyncio.ensure_future(
            asyncio.to_thread(self.agent_runner, **runner_kwargs)
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
            shutil.rmtree(output_dir, ignore_errors=True)
            return

        try:
            await self._emit_produced_artifacts(task_id=task_id, output_dir=output_dir)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

        await self.send(build_task_complete_frame(task_id=task_id, text=answer))

    def _reassemble_inbound_files(self, *, task_id: str, output_dir: Path) -> List[Path]:
        """Reassemble any inbound artifact buffers for ``task_id`` (Task
        2.5) into real files under ``output_dir``, and return their paths.
        Verifies SHA-256 when the ``artifact_begin`` frame declared one.
        """
        buffers = self._inbound_artifact_buffers.pop(task_id, {})
        input_dir = output_dir / "inbound"
        paths: List[Path] = []
        for artifact_id, buf in buffers.items():
            begin = buf["begin"]
            data = reassemble_artifact_chunks(buf["chunks"])
            declared_digest = str(begin.get("sha256") or "")
            if declared_digest:
                import hashlib

                actual_digest = hashlib.sha256(data).hexdigest()
                if actual_digest != declared_digest:
                    logger.warning(
                        "task %s: inbound artifact %s failed SHA-256 verification; skipping",
                        task_id,
                        artifact_id,
                    )
                    continue
            input_dir.mkdir(parents=True, exist_ok=True)
            name = str(begin.get("name") or f"upload_{artifact_id}.bin")
            path = input_dir / Path(name).name
            path.write_bytes(data)
            paths.append(path)
        return paths

    async def _emit_produced_artifacts(self, *, task_id: str, output_dir: Path) -> None:
        """Scan ``output_dir`` for files the agent turn produced and emit
        them as artifact frames: inline for small files, chunked for large
        ones (Task 2.3)."""
        if not output_dir.exists():
            return
        for path in sorted(p for p in output_dir.iterdir() if p.is_file()):
            data = path.read_bytes()
            artifact_id = f"art_{uuid.uuid4().hex[:12]}"
            if len(data) <= INLINE_MAX_BYTES:
                await self.send(
                    build_task_artifact_frame(
                        task_id=task_id,
                        artifact_id=artifact_id,
                        name=path.name,
                        data=data,
                        mime_type="application/octet-stream",
                    )
                )
            else:
                import hashlib

                digest = hashlib.sha256(data).hexdigest()
                await self.send(
                    build_artifact_begin_frame(
                        task_id=task_id,
                        artifact_id=artifact_id,
                        name=path.name,
                        mime_type="application/octet-stream",
                        total_bytes=len(data),
                        sha256=digest,
                    )
                )
                chunk_count = 0
                for seq, chunk in enumerate(chunk_artifact_bytes(data, chunk_bytes=self.chunk_bytes)):
                    await self.send(
                        build_artifact_chunk_frame(
                            task_id=task_id, artifact_id=artifact_id, seq=seq, data=chunk
                        )
                    )
                    chunk_count += 1
                await self.send(build_artifact_end_frame(task_id=task_id, artifact_id=artifact_id))
                logger.info(
                    "task %s: emitted artifact %s (%d bytes) as %d artifact_chunk frames",
                    task_id,
                    artifact_id,
                    len(data),
                    chunk_count,
                )
