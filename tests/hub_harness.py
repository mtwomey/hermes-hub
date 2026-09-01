"""In-process live-hub harness for the W3 tool tests.

The ``peer_*`` tool handlers are synchronous and drive a real HTTP client
(``asyncio.run`` inside), so they cannot share an event loop with Starlette's
``TestClient`` portal. This harness therefore runs the **real** hub ASGI app
under uvicorn on an ephemeral port in a background thread, and attaches a
fake spoke connection directly to that app's :class:`Router` so the spoke's
reply frames are dispatched on the server's own loop.

Everything the tools touch — HTTP, SSE, JSON-RPC, the artifact download
route — is the genuine hub surface. Only the spoke's *agent* is faked.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import uvicorn

from hermes_hub.hub_server import build_hub_app
from hermes_hub.protocol import (
    build_artifact_begin_frame,
    build_artifact_chunk_frame,
    build_artifact_end_frame,
    build_task_artifact_frame,
    build_task_complete_frame,
    build_task_failed_frame,
    build_task_status_frame,
    chunk_artifact_bytes,
)
from hermes_hub.registry import SpokeRegistry
from hermes_hub.router import Router


class FakeSpokeConnection:
    """A spoke that answers task frames on the hub's own event loop.

    ``expected_credential`` mirrors the real ``SpokeExecutor``'s V5a check:
    when set, a task frame carrying a different credential is failed before
    any "agent" work happens.
    """

    def __init__(
        self,
        *,
        router: Router,
        name: str,
        reply: Callable[[Dict[str, Any]], str] | str = "ok",
        expected_credential: str = "",
        artifact: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.router = router
        self.name = name
        self.reply = reply
        self.expected_credential = expected_credential
        self.artifact = artifact
        #: Every task frame this spoke received, verbatim. The credential
        #: leak-canary tests read this to prove the credential *did* arrive.
        self.received: List[Dict[str, Any]] = []

    async def send(self, frame: Dict[str, Any]) -> None:
        self.received.append(frame)
        if frame.get("type") != "task":
            return
        asyncio.get_running_loop().create_task(self._respond(frame))

    async def _respond(self, frame: Dict[str, Any]) -> None:
        task_id = frame["task_id"]
        presented = str(frame.get("credential") or "")
        if self.expected_credential and presented != self.expected_credential:
            await self.router.dispatch_frame_from_spoke(
                build_task_failed_frame(
                    task_id=task_id, error="unauthorized: credential rejected by spoke"
                )
            )
            return
        await self.router.dispatch_frame_from_spoke(
            build_task_status_frame(task_id=task_id)
        )
        if self.artifact is not None:
            data = self.artifact["data"]
            if self.artifact.get("chunked"):
                # Large-artifact path: begin/chunk*/end. The hub's Router
                # reassembles, verifies SHA-256, stores the bytes, and
                # synthesizes a task_artifact frame carrying a download URL.
                # This is the only path that produces a fetchable artifact.
                artifact_id = self.artifact.get("artifact_id", "art1")
                await self.router.dispatch_frame_from_spoke(
                    build_artifact_begin_frame(
                        task_id=task_id,
                        artifact_id=artifact_id,
                        name=self.artifact.get("name", "blob.bin"),
                        mime_type=self.artifact.get(
                            "mime_type", "application/octet-stream"
                        ),
                        total_bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )
                for seq, chunk in enumerate(chunk_artifact_bytes(data)):
                    await self.router.dispatch_frame_from_spoke(
                        build_artifact_chunk_frame(
                            task_id=task_id,
                            artifact_id=artifact_id,
                            seq=seq,
                            data=chunk,
                        )
                    )
                await self.router.dispatch_frame_from_spoke(
                    build_artifact_end_frame(task_id=task_id, artifact_id=artifact_id)
                )
            else:
                await self.router.dispatch_frame_from_spoke(
                    {
                        "type": "task_artifact",
                        "task_id": task_id,
                        "artifact_id": self.artifact.get("artifact_id", "art1"),
                        "name": self.artifact.get("name", "blob.bin"),
                        "mime_type": self.artifact.get(
                            "mime_type", "application/octet-stream"
                        ),
                        "data": base64.b64encode(data).decode("ascii"),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "text": "",
                    }
                )
        text = self.reply(frame) if callable(self.reply) else self.reply
        await self.router.dispatch_frame_from_spoke(
            build_task_complete_frame(task_id=task_id, text=text)
        )


class LiveHub:
    """A real uvicorn-hosted hub on an ephemeral port."""

    def __init__(
        self,
        *,
        spokes: Optional[List[Dict[str, Any]]] = None,
        external_token: str = "",
        artifact_root: Optional[Any] = None,
    ) -> None:
        self.registry = SpokeRegistry()
        self.router = Router()
        self.external_token = external_token
        self.connections: Dict[str, FakeSpokeConnection] = {}
        self._spoke_specs = spokes or []
        self._artifact_root = artifact_root
        self._saved_artifact_root: Optional[Callable[[], Any]] = None
        self.port = 0
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "LiveHub":
        if self._artifact_root is not None:
            from pathlib import Path as _Path

            from hermes_hub import artifacts as _artifacts

            root = _Path(self._artifact_root)
            root.mkdir(parents=True, exist_ok=True)
            self._saved_artifact_root = _artifacts._artifact_root
            _artifacts._artifact_root = lambda: root
        app = build_hub_app(
            registry=self.registry,
            router=self.router,
            base_url="",  # filled in after the port is known
            expected_external_token=self.external_token,
            task_timeout_seconds=20.0,
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._server.started and self._server.servers:
                sockets = self._server.servers[0].sockets
                if sockets:
                    self.port = sockets[0].getsockname()[1]
                    break
            time.sleep(0.02)
        if not self.port:
            raise RuntimeError("hub failed to start")
        self.router.base_url = self.base_url
        for spec in self._spoke_specs:
            self.add_spoke(**spec)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._saved_artifact_root is not None:
            from hermes_hub import artifacts as _artifacts

            _artifacts._artifact_root = self._saved_artifact_root
            self._saved_artifact_root = None

    def add_spoke(
        self,
        *,
        name: str,
        skills: Optional[List[Dict[str, Any]]] = None,
        reply: Callable[[Dict[str, Any]], str] | str = "ok",
        expected_credential: str = "",
        artifact: Optional[Dict[str, Any]] = None,
    ) -> FakeSpokeConnection:
        self.registry.register(name=name, skills=list(skills or []))
        conn = FakeSpokeConnection(
            router=self.router,
            name=name,
            reply=reply,
            expected_credential=expected_credential,
            artifact=artifact,
        )
        self.router.register_connection(name, conn)
        self.connections[name] = conn
        return conn
