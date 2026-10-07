"""HTTP client for a running hermes-hub's external A2A surface (W3, M1).

Ported in shape from hermes-peer's ``hermes_peer/client.py`` (V14: port
working code, don't reimplement) and adapted from mesh to hub-and-spoke:

  hermes-peer                     hermes-hub
  -----------                     ----------
  one base URL per peer           one base URL: the hub
  peer named by config key        spoke named in message metadata
                                  (``targetSpoke``, H6)
  per-peer bearer token           hub's single external bearer token, plus
                                  the caller's opaque per-spoke credential
                                  relayed in ``metadata.spokeCredential``
                                  (V5a) which the SPOKE checks, not the hub
  /a2a/artifacts/{id}             /a2a/artifacts/{task_id}/{artifact_id}
                                  (task-scoped: omitting task_id is a 404)

Deliberately a plain ``httpx`` JSON-RPC client rather than the SDK's
``ClientFactory``/transport-negotiation layer, matching hermes-peer's
reasoning: the binding is fixed to JSON-RPC, so negotiation is unneeded
surface.

**Credential discipline (V5a):** ``credential`` is treated as opaque bytes.
This module never parses it, never logs it, and never returns it in any
result dict — see the leak-canary tests.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
import hashlib
import json as jsonlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .caller_contract import META_SPOKE_CREDENTIAL, META_TARGET_SPOKE

#: Every A2A HTTP request needs this header.
A2A_VERSION_HEADER = {"A2A-Version": "1.0"}

#: Per-HTTP-request timeout. Since Phase 1 (BEA-304) every call is short --
#: SendMessage(returnImmediately) or GetTask -- so this is no longer coupled
#: to how long a task runs (previously 330 s > the hub's 300 s timeout).
DEFAULT_TIMEOUT_SECONDS = 30.0

#: GetTask polling interval used by :meth:`HubClient.wait`.
DEFAULT_POLL_SECONDS = 1.0

#: How long ``ask`` waits client-side before returning ``state=working`` (D2).
DEFAULT_WAIT_SECONDS = 270.0


class HubClientError(RuntimeError):
    """Any failure reaching, or reported by, the hub or a spoke."""


class HubClient:
    """Async client for the hub's A2A surface."""

    def __init__(
        self,
        *,
        hub_url: str,
        token: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self._token = token
        self.timeout_seconds = timeout_seconds

    # -- helpers -------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", **A2A_VERSION_HEADER}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.hub_url, timeout=self.timeout_seconds)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise HubClientError(f"{resp.status_code}: {resp.text}")

    # -- verbs ---------------------------------------------------------------

    async def agent_card(self) -> Dict[str, Any]:
        """Fetch the hub's aggregate AgentCard.

        The card is rebuilt from the live spoke registry on every request, so
        this doubles as "which spokes are connected right now" — the source
        for ``peer_list``/``peer_info``. Skills are namespaced
        ``<spoke>::<skill-id>`` and carry the spoke's own description text
        (Task 1.3), which is what makes the card useful to a model rather
        than just to a router.
        """
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/.well-known/agent-card.json", headers=self._headers()
                )
                self._raise_for_status(resp)
                return resp.json()
        except httpx.HTTPError as exc:
            raise HubClientError(f"hub is unreachable at {self.hub_url}: {exc}") from exc

    def _build_message(
        self,
        spoke_name: str,
        text: str,
        *,
        context_id: str,
        credential: str,
        file_name: str,
        file_bytes: Optional[bytes],
        file_mime_type: str,
    ) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {META_TARGET_SPOKE: spoke_name}
        if credential:
            metadata[META_SPOKE_CREDENTIAL] = credential
        parts: List[Dict[str, Any]] = [{"text": text}]
        if file_bytes is not None:
            parts.append(
                {
                    "raw": base64.b64encode(file_bytes).decode("ascii"),
                    "filename": file_name or "upload.bin",
                    "media_type": file_mime_type,
                }
            )
        message: Dict[str, Any] = {
            "role": "ROLE_USER",
            "parts": parts,
            "messageId": f"hub-{uuid.uuid4().hex}",
            "metadata": metadata,
        }
        if context_id:
            message["contextId"] = context_id
        return message

    async def submit(
        self,
        spoke_name: str,
        text: str,
        *,
        context_id: str = "",
        credential: str = "",
        file_name: str = "",
        file_bytes: Optional[bytes] = None,
        file_mime_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        """Phase 1.4: start a task without waiting for it (A2A ``SendMessage``
        with ``configuration.returnImmediately=true``). The hub runs the task
        to a terminal state regardless of this caller (D1); fetch it later
        with :meth:`wait` / :meth:`get_task`. Returns ``task_id``,
        ``context_id`` and ``state``.

        ``credential`` (V5a) travels in the message metadata under
        ``spokeCredential`` and is omitted entirely when empty.
        """
        message = self._build_message(
            spoke_name,
            text,
            context_id=context_id,
            credential=credential,
            file_name=file_name,
            file_bytes=file_bytes,
            file_mime_type=file_mime_type,
        )
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {"message": message, "configuration": {"returnImmediately": True}},
        }
        payload = await self._rpc(body)
        task = payload.get("task") or payload
        return {
            "task_id": str(task.get("id") or ""),
            "context_id": str(task.get("contextId") or context_id or ""),
            "state": _short_state((task.get("status") or {}).get("state", "")),
        }

    async def wait(
        self,
        task_id: str,
        seconds: float = DEFAULT_WAIT_SECONDS,
        *,
        poll_interval: float = DEFAULT_POLL_SECONDS,
    ) -> Dict[str, Any]:
        """Poll ``GetTask`` until the task is terminal or ``seconds`` elapse.

        Never raises because the deadline passed: returns the task summary
        with ``state`` still ``working``/``submitted`` (D2)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(seconds))
        while True:
            summary = summarize_task(await self.get_task(task_id), task_id)
            if summary["state"] in TERMINAL_STATES:
                return summary
            remaining = deadline - loop.time()
            if remaining <= 0:
                return summary
            await asyncio.sleep(min(poll_interval, remaining))

    async def ask(
        self,
        spoke_name: str,
        text: str,
        *,
        context_id: str = "",
        credential: str = "",
        file_name: str = "",
        file_bytes: Optional[bytes] = None,
        file_mime_type: str = "application/octet-stream",
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
    ) -> Dict[str, Any]:
        """``submit`` + ``wait(wait_seconds)``.

        Returns a compact summary — ``state``, ``text``, ``task_id``,
        ``context_id``, ``elapsed_s`` and an ``artifacts`` list — never the raw
        JSON-RPC/protobuf envelope (Task 1.2). If the task is still running
        at the deadline, ``state`` is ``working`` (not an error); a FAILED
        task raises :class:`HubClientError` with the hub's error text.
        """
        submitted = await self.submit(
            spoke_name,
            text,
            context_id=context_id,
            credential=credential,
            file_name=file_name,
            file_bytes=file_bytes,
            file_mime_type=file_mime_type,
        )
        summary = await self.wait(submitted["task_id"], wait_seconds)
        if not summary["context_id"]:
            summary["context_id"] = submitted["context_id"]
        if summary["state"] in FAILURE_STATES:
            raise HubClientError(summary.get("error") or summary["text"] or "task failed")
        return summary

    async def _rpc(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with self._client() as client:
                resp = await client.post("/a2a/v1", json=body, headers=self._headers())
                self._raise_for_status(resp)
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise HubClientError(f"hub is unreachable at {self.hub_url}: {exc}") from exc
        if "error" in payload:
            raise HubClientError(str(payload["error"]))
        return payload.get("result", {})

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """A2A ``GetTask``: read one task's current state by id."""
        return await self._rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task_id}}
        )

    async def download_artifact(
        self,
        task_id: str,
        artifact_id: str,
        destination: Optional[Path] = None,
        *,
        expected_sha256: str = "",
    ) -> Path:
        """Download an artifact from the hub and verify its SHA-256.

        The route is task-scoped: ``/a2a/artifacts/{task_id}/{artifact_id}``.
        Omitting ``task_id`` is a 404, not a lookup by artifact id alone.
        """
        url = f"/a2a/artifacts/{task_id}/{artifact_id}"
        try:
            async with self._client() as client:
                resp = await client.get(url, headers=self._headers())
                self._raise_for_status(resp)
                data = resp.content
        except httpx.HTTPError as exc:
            raise HubClientError(f"hub is unreachable at {self.hub_url}: {exc}") from exc

        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise HubClientError(
                f"artifact {artifact_id} failed SHA-256 verification: "
                f"expected {expected_sha256}, got {digest}"
            )
        if destination is None:
            destination = Path.cwd() / artifact_id
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination


def _artifact_from_event(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Summarize an artifact-update SSE event into compact metadata.

    Returns ``None`` for any event that is not an artifact update. Inline
    bytes are deliberately NOT returned — only their size — so a large
    binary can never end up in the model's context (Task 1.2).
    """
    update = result.get("artifactUpdate") or result.get("artifact_update")
    if not update:
        return None
    artifact = update.get("artifact") or {}
    metadata = artifact.get("metadata") or {}
    inline_len = 0
    for part in artifact.get("parts", []) or []:
        if part.get("raw"):
            inline_len = len(base64.b64decode(part["raw"]))
            break
    size = metadata.get("size_bytes") or metadata.get("sizeBytes") or inline_len
    return {
        "artifact_id": artifact.get("artifactId") or artifact.get("artifact_id") or "",
        "name": artifact.get("name") or "",
        "sha256": metadata.get("sha256") or "",
        "url": metadata.get("url") or "",
        "size_bytes": int(size or 0),
        "inline": inline_len > 0,
    }


TERMINAL_STATES = ("completed", "failed", "canceled", "rejected")
FAILURE_STATES = ("failed", "canceled", "rejected")


def _short_state(state: Any) -> str:
    state = str(state or "")
    return state[len("TASK_STATE_") :].lower() if state.startswith("TASK_STATE_") else state.lower()


def _elapsed_s(metadata: Dict[str, Any]) -> int:
    from datetime import datetime, timezone

    started = metadata.get("startedAt")
    if not started:
        return 0
    try:
        t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - t0).total_seconds()))


def summarize_task(task: Dict[str, Any], task_id: str = "") -> Dict[str, Any]:
    """Compact, model-safe view of an A2A Task (GetTask result): state, final
    text, artifacts (metadata only, never bytes), hermesError on failure."""
    status = task.get("status") or {}
    message = status.get("message") or {}
    text = "".join(p.get("text", "") for p in message.get("parts", []) or [])
    state = _short_state(status.get("state"))
    resolved_id = str(task.get("id") or task_id)
    artifacts: List[Dict[str, Any]] = []
    for raw_artifact in task.get("artifacts") or []:
        summary = _artifact_from_event({"artifactUpdate": {"artifact": raw_artifact}})
        if summary is not None:
            summary["task_id"] = resolved_id
            artifacts.append(summary)
    out: Dict[str, Any] = {
        "state": state,
        "text": text if state == "completed" else "",
        "task_id": resolved_id,
        "context_id": str(task.get("contextId") or task.get("context_id") or ""),
        "artifacts": artifacts,
        "elapsed_s": _elapsed_s(task.get("metadata") or {}),
    }
    if state in FAILURE_STATES:
        out["error"] = text or "task failed"
        hermes_error = (message.get("metadata") or {}).get("hermesError")
        if hermes_error:
            out["hermes_error"] = str(hermes_error)
    return out
