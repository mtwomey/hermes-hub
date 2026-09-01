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

import base64
import hashlib
import json as jsonlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

#: Every A2A HTTP request needs this header.
A2A_VERSION_HEADER = {"A2A-Version": "1.0"}

DEFAULT_TIMEOUT_SECONDS = 180.0


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
            raise HubClientError(f"cannot reach hub at {self.hub_url}: {exc}") from exc

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
    ) -> Dict[str, Any]:
        """Send a request to ``spoke_name`` through the hub and return its reply.

        Returns a compact summary — ``text``, ``task_id``, ``context_id``,
        and an ``artifacts`` list of id/name/sha256/url/size — never the raw
        JSON-RPC/protobuf envelope (Task 1.2).

        ``credential`` (V5a) travels in the message metadata under
        ``spokeCredential``, exactly where ``hub_executor`` extracts it from,
        and is omitted entirely when empty so a caller with nothing
        configured produces the same request shape as before this existed.
        """
        metadata: Dict[str, Any] = {"targetSpoke": spoke_name}
        if credential:
            metadata["spokeCredential"] = credential
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
            "messageId": f"hub-{abs(hash((spoke_name, text))) & 0xFFFFFFFF:x}",
            "metadata": metadata,
        }
        if context_id:
            message["contextId"] = context_id
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendStreamingMessage",
            "params": {"message": message},
        }

        final_text = ""
        final_task_id = ""
        final_context_id = context_id
        failure: Optional[str] = None
        artifacts: List[Dict[str, Any]] = []

        try:
            async with self._client() as client:
                async with client.stream(
                    "POST", "/a2a/v1", json=body, headers=self._headers()
                ) as resp:
                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        raise HubClientError(
                            f"{resp.status_code}: {raw.decode(errors='replace')}"
                        )
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = jsonlib.loads(line[len("data:") :].strip())
                        if "error" in payload:
                            raise HubClientError(str(payload["error"]))
                        result = payload.get("result", {})

                        artifact = _artifact_from_event(result)
                        if artifact is not None:
                            artifacts.append(artifact)

                        task = (
                            result.get("task")
                            or result.get("statusUpdate")
                            or result.get("status_update")
                        )
                        if not task:
                            continue
                        final_task_id = (
                            task.get("id") or task.get("taskId") or final_task_id
                        )
                        final_context_id = (
                            task.get("contextId")
                            or task.get("context_id")
                            or final_context_id
                        )
                        status = task.get("status", {})
                        state = status.get("state", "")
                        message_out = status.get("message")
                        rendered = ""
                        if message_out:
                            rendered = "".join(
                                p.get("text", "") for p in message_out.get("parts", [])
                            )
                        if state == "TASK_STATE_COMPLETED":
                            final_text = rendered or final_text
                        elif state == "TASK_STATE_FAILED":
                            failure = rendered or "task failed"
        except HubClientError:
            raise
        except httpx.HTTPError as exc:
            raise HubClientError(f"cannot reach hub at {self.hub_url}: {exc}") from exc

        if failure is not None:
            raise HubClientError(failure)

        for artifact in artifacts:
            artifact.setdefault("task_id", final_task_id)

        return {
            "text": final_text,
            "task_id": final_task_id,
            "context_id": final_context_id,
            "artifacts": artifacts,
        }

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """A2A ``GetTask``: read one task's current state by id."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "GetTask",
            "params": {"id": task_id},
        }
        try:
            async with self._client() as client:
                resp = await client.post("/a2a/v1", json=body, headers=self._headers())
                self._raise_for_status(resp)
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise HubClientError(f"cannot reach hub at {self.hub_url}: {exc}") from exc
        if "error" in payload:
            raise HubClientError(str(payload["error"]))
        return payload.get("result", {})

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
            raise HubClientError(f"cannot reach hub at {self.hub_url}: {exc}") from exc

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
