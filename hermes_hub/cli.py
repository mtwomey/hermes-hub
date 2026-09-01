"""``hermes-hub`` / ``hermes-hub-spoke`` CLIs (M6).

Two console-script entry points (see ``pyproject.toml``):

  hermes-hub serve [--host HOST] [--port PORT] [--token TOKEN]
      Runs the hub's ASGI app (agent-card, JSON-RPC, spoke WebSocket
      endpoint) under uvicorn.

  hermes-hub ask <spoke> "<text>" [--hub-url URL] [--context-id ID]
      H6: address a specific spoke by name. Sends SendStreamingMessage to
      the hub's external A2A endpoint and renders SSE frames live.

  hermes-hub-spoke connect --hub <url> --name <name> --token <token>
      Runs a real spoke process: connects out to the hub, executes routed
      tasks with a real Hermes agent turn (spoke_executor.run_real_hermes_turn).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

import httpx


# -- hermes-hub: hub process + external CLI verbs ----------------------------


def build_hub_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-hub")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the hub server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument(
        "--token", default="", help="Bearer token external A2A callers must present"
    )
    serve.add_argument(
        "--spoke-token", default="", help="Bearer token spokes must present at connect time"
    )

    ask = sub.add_parser("ask", help="Ask a named spoke a question through the hub")
    ask.add_argument("spoke", help="Name of the target spoke (H6)")
    ask.add_argument("text", help="The question/prompt text")
    ask.add_argument("--hub-url", default="http://127.0.0.1:8770")
    ask.add_argument("--context-id", default="", help="Reuse a prior contextId for continuity")
    ask.add_argument("--token", default="", help="Bearer token for the hub's external API")
    ask.add_argument(
        "--credential",
        default="",
        help="Opaque per-spoke credential (V5a) the target spoke will check",
    )
    ask.add_argument(
        "--file",
        default="",
        help="Path to a local file to send to the spoke inline (Task 2.5, V9)",
    )
    ask.add_argument(
        "--save-artifacts-to",
        default="",
        help="Directory to save any returned artifacts into, by name (Task 2.4)",
    )

    return parser


def build_streaming_message_body(
    *,
    spoke: str,
    text: str,
    context_id: str = "",
    credential: str = "",
    file_name: str = "",
    file_bytes: Optional[bytes] = None,
    file_mime_type: str = "application/octet-stream",
) -> Dict[str, Any]:
    """The JSON-RPC ``SendStreamingMessage`` body for ``hermes-hub ask``.

    ``credential`` (V5/V5a): the caller's opaque per-spoke secret, carried in
    message metadata under ``spokeCredential`` -- the same key
    ``hub_executor`` extracts it from. Omitted entirely when empty so a
    caller with no credential configured produces exactly the same request
    shape as before this feature existed.

    ``file_bytes`` (Task 2.5, V9): an inbound file to send to the spoke,
    inline base64 in a ``raw`` A2A Part alongside the text part.
    """
    metadata: Dict[str, Any] = {"targetSpoke": spoke}
    if credential:
        metadata["spokeCredential"] = credential
    parts: List[Dict[str, Any]] = [{"text": text}]
    if file_bytes is not None:
        import base64

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
        "messageId": f"cli-{abs(hash(text)) & 0xFFFFFFFF:x}",
        "metadata": metadata,
    }
    if context_id:
        message["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendStreamingMessage",
        "params": {"message": message},
    }


def parse_sse_event_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one SSE ``data:`` line into its JSON-RPC payload, or None."""
    if not line.startswith("data:"):
        return None
    return json.loads(line[len("data:") :].strip())


def extract_final_text(payload: Dict[str, Any]) -> Optional[str]:
    """Pull the final answer text out of a completed task's status update, if present."""
    result = payload.get("result", {})
    status_update = result.get("statusUpdate") or {}
    status = status_update.get("status", {})
    if status.get("state") != "TASK_STATE_COMPLETED":
        return None
    message = status.get("message") or {}
    parts = message.get("parts", [])
    return "".join(p.get("text", "") for p in parts) or None


def extract_artifact_info(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull artifact metadata (name, url, sha256, size_bytes, inline bytes)
    out of an SSE event carrying an artifact-update result, if present
    (Task 2.4/2.5)."""
    result = payload.get("result", {})
    artifact_update = result.get("artifactUpdate") or result.get("artifact_update")
    if not artifact_update:
        return None
    artifact = artifact_update.get("artifact") or {}
    metadata = artifact.get("metadata") or {}
    inline_bytes = None
    for part in artifact.get("parts", []):
        if part.get("raw"):
            import base64

            inline_bytes = base64.b64decode(part["raw"])
            break
    return {
        "name": artifact.get("name"),
        "artifact_id": artifact.get("artifactId") or artifact.get("artifact_id"),
        "url": metadata.get("url"),
        "sha256": metadata.get("sha256"),
        "size_bytes": metadata.get("size_bytes") or metadata.get("sizeBytes"),
        "inline_bytes": inline_bytes,
    }


async def _ask(args: argparse.Namespace) -> int:
    file_bytes = None
    file_name = ""
    if getattr(args, "file", ""):
        import mimetypes
        from pathlib import Path as _Path

        p = _Path(args.file)
        file_bytes = p.read_bytes()
        file_name = p.name
        file_mime_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    else:
        file_mime_type = "application/octet-stream"

    body = build_streaming_message_body(
        spoke=args.spoke,
        text=args.text,
        context_id=args.context_id,
        credential=args.credential,
        file_name=file_name,
        file_bytes=file_bytes,
        file_mime_type=file_mime_type,
    )
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    final_text = None
    async with httpx.AsyncClient(base_url=args.hub_url, timeout=180.0) as client:
        async with client.stream("POST", "/a2a/v1", json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                text = await resp.aread()
                print(f"error: {resp.status_code}: {text.decode(errors='replace')}", file=sys.stderr)
                return 1
            async for line in resp.aiter_lines():
                payload = parse_sse_event_line(line)
                if payload is None:
                    continue
                if "error" in payload:
                    print(f"error: {payload['error']}", file=sys.stderr)
                    return 1
                artifact_info = extract_artifact_info(payload)
                if artifact_info is not None:
                    printable = {k: v for k, v in artifact_info.items() if k != "inline_bytes"}
                    printable["has_inline_bytes"] = artifact_info.get("inline_bytes") is not None
                    print(f"artifact: {json.dumps(printable)}", file=sys.stderr)
                    save_dir = getattr(args, "save_artifacts_to", "")
                    if save_dir and artifact_info.get("name"):
                        from pathlib import Path as _Path

                        dest_dir = _Path(save_dir)
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest = dest_dir / artifact_info["name"]
                        if artifact_info.get("inline_bytes") is not None:
                            dest.write_bytes(artifact_info["inline_bytes"])
                            print(f"saved inline artifact to {dest}", file=sys.stderr)
                        elif artifact_info.get("url"):
                            url = artifact_info["url"]
                            if url.startswith("/"):
                                url = args.hub_url.rstrip("/") + url
                            dl_headers = dict(headers)
                            async with httpx.AsyncClient(timeout=180.0) as dl_client:
                                dl_resp = await dl_client.get(url, headers=dl_headers)
                                dl_resp.raise_for_status()
                                dest.write_bytes(dl_resp.content)
                            print(f"downloaded artifact to {dest}", file=sys.stderr)
                text = extract_final_text(payload)
                if text is not None:
                    final_text = text
    if final_text is not None:
        print(final_text)
        return 0
    print("error: no final answer received", file=sys.stderr)
    return 1


def hub_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_hub_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from .hub_server import build_hub_app

        app = build_hub_app(
            base_url=f"http://{args.host}:{args.port}",
            expected_external_token=args.token,
            expected_spoke_token=args.spoke_token,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "ask":
        return asyncio.run(_ask(args))
    parser.error(f"unknown command: {args.command}")
    return 2


# -- hermes-hub-spoke: spoke process CLI -------------------------------------


def build_spoke_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-hub-spoke")
    sub = parser.add_subparsers(dest="command", required=True)

    connect = sub.add_parser("connect", help="Connect to a hub as a spoke")
    connect.add_argument("--hub", required=True, help="Hub base URL, e.g. ws://hub.example.invalid:8770")
    connect.add_argument("--name", required=True, help="This spoke's name")
    connect.add_argument("--token", default="", help="Bearer token to present at connect time")
    connect.add_argument(
        "--skill",
        action="append",
        default=[],
        dest="skills",
        help="skill_id:Human Name, repeatable",
    )
    return parser


def parse_skill_args(skill_args: List[str]) -> List[Dict[str, str]]:
    skills = []
    for raw in skill_args:
        if ":" in raw:
            skill_id, name = raw.split(":", 1)
        else:
            skill_id, name = raw, raw
        skills.append({"id": skill_id, "name": name})
    return skills


def spoke_hub_ws_url(hub_base: str) -> str:
    """Turn a hub base URL (http(s) or ws(s), with or without a path) into
    the exact spoke WebSocket URL."""
    url = hub_base.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    if not url.endswith("/hub/v1/spoke"):
        url = url + "/hub/v1/spoke"
    return url


async def _connect(args: argparse.Namespace) -> int:
    from .credentials import resolve_spoke_credential
    from .sessions import SessionMap
    from .spoke_client import SpokeClient
    from .spoke_executor import SpokeExecutor

    client_holder: Dict[str, Any] = {}

    async def send(frame: Dict[str, Any]) -> None:
        await client_holder["client"].send(frame)

    expected_credential = resolve_spoke_credential(args.name)
    executor = SpokeExecutor(
        spoke_name=args.name,
        send=send,
        session_map=SessionMap(),
        expected_credential=expected_credential,
    )

    async def on_frame(frame: Dict[str, Any]) -> None:
        await executor.handle_frame(frame)

    client = SpokeClient(
        hub_url=spoke_hub_ws_url(args.hub),
        name=args.name,
        token=args.token,
        skills=parse_skill_args(args.skills),
        on_frame=on_frame,
    )
    client_holder["client"] = client
    await client.run()
    return 0


def spoke_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_spoke_parser()
    args = parser.parse_args(argv)
    if args.command == "connect":
        return asyncio.run(_connect(args))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(hub_main())
