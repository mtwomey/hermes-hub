"""The hub's ASGI application: external A2A surface + inbound spoke WebSocket
endpoint (M3).

Two independent route groups on one Starlette app:
  - External-facing A2A surface (unchanged shape from hermes-peer, H4):
    ``/.well-known/agent-card.json``, JSON-RPC ``/a2a/v1``. The card is
    rebuilt from the live :class:`SpokeRegistry` on every request via
    ``card_modifier`` so it always reflects currently-connected spokes (H5).
  - ``ws://<hub>:<port>/hub/v1/spoke`` -- the inbound WebSocket endpoint
    spokes connect to (H2: only the hub binds a listening socket). Each
    connection's first frame must be a ``register`` frame; anything else is
    rejected. After registration the connection stays in a receive loop,
    forwarding non-registration frames to the :class:`Router`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from .agent_card import build_hub_agent_card
from .hub_executor import HubExecutor
from .registry import SpokeRegistry
from .router import Router

logger = logging.getLogger("hermes_hub.hub_server")

SPOKE_WS_PATH = "/hub/v1/spoke"


class WebSocketSpokeConnection:
    """Adapts a live Starlette ``WebSocket`` to the Router's send-only
    connection protocol."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send(self, frame: Dict[str, Any]) -> None:
        await self._websocket.send_text(json.dumps(frame))


def _check_token(expected_token: str, presented_token: str) -> bool:
    if not expected_token:
        return True  # dev mode: no token configured, allow (mirrors hermes-peer's D5 behavior)
    return presented_token == expected_token


class ExternalBearerAuthMiddleware:
    """Enforce a shared bearer token on the hub's external HTTP routes only.

    Mirrors hermes-peer's ``BearerAuthMiddleware`` (D5): an empty configured
    token means "allow" (dev mode); a configured token rejects a missing or
    wrong ``Authorization`` header with 401. Scoped to HTTP requests only —
    the spoke WebSocket endpoint has its own independent token check
    (``expected_spoke_token``, H8) and must not be double-gated here.
    """

    def __init__(self, app: ASGIApp, *, expected_token: str) -> None:
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.expected_token:
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        if headers.get("authorization", "") != f"Bearer {self.expected_token}":
            response = JSONResponse(
                {"error": "unauthorized", "message": "A valid bearer token is required."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_hub_app(
    *,
    registry: Optional[SpokeRegistry] = None,
    router: Optional[Router] = None,
    hub_name: str = "hermes-hub",
    base_url: str = "http://127.0.0.1:8770",
    expected_spoke_token: str = "",
    expected_external_token: str = "",
    task_timeout_seconds: float = 300.0,
) -> Starlette:
    """Build the hub's ASGI app: A2A surface + spoke WebSocket endpoint."""
    registry = registry or SpokeRegistry()
    router = router or Router(base_url=base_url)

    executor = HubExecutor(router=router, timeout_seconds=task_timeout_seconds)
    base_card = build_hub_agent_card(registry, hub_name=hub_name, base_url=base_url)

    async def card_modifier(card):
        # Rebuild the card fresh from the live registry on every request so
        # it always reflects currently-connected spokes (H5), not a stale
        # snapshot taken at process startup.
        return build_hub_agent_card(registry, hub_name=hub_name, base_url=base_url)

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=base_card,
    )

    routes = list(create_agent_card_routes(base_card, card_modifier=card_modifier))
    routes.extend(create_jsonrpc_routes(handler, rpc_url="/a2a/v1"))

    async def spoke_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        spoke_name: Optional[str] = None
        try:
            raw = await websocket.receive_text()
            frame = json.loads(raw)
            if frame.get("type") != "register":
                await websocket.close(code=4400, reason="first frame must be 'register'")
                return
            presented_token = str(frame.get("token") or "")
            if not _check_token(expected_spoke_token, presented_token):
                logger.warning("rejected spoke registration: bad token")
                await websocket.close(code=4401, reason="invalid token")
                return
            spoke_name = str(frame.get("name") or "")
            if not spoke_name:
                await websocket.close(code=4400, reason="missing spoke name")
                return
            skills = frame.get("skills") or []
            registry.register(name=spoke_name, skills=skills)
            router.register_connection(spoke_name, WebSocketSpokeConnection(websocket))
            logger.info("spoke registered: %s", spoke_name)

            while True:
                raw = await websocket.receive_text()
                inbound_frame = json.loads(raw)
                await router.dispatch_frame_from_spoke(inbound_frame)
        except WebSocketDisconnect:
            pass
        finally:
            if spoke_name:
                registry.deregister(spoke_name)
                router.unregister_connection(spoke_name)
                logger.info("spoke disconnected: %s", spoke_name)

    routes.append(WebSocketRoute(SPOKE_WS_PATH, endpoint=spoke_endpoint))

    async def download_artifact(request):
        task_id = request.path_params["task_id"]
        artifact_id = request.path_params["artifact_id"]
        from . import artifacts as _artifacts

        ref = _artifacts.load_artifact_metadata(task_id, artifact_id)
        if ref is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        from starlette.responses import FileResponse

        return FileResponse(ref.path, media_type=ref.mime_type, filename=ref.name)

    from .artifacts import ARTIFACT_DOWNLOAD_PATH

    routes.append(
        Route(
            f"{ARTIFACT_DOWNLOAD_PATH}/{{task_id}}/{{artifact_id}}",
            endpoint=download_artifact,
            methods=["GET"],
        )
    )

    async def health(request):
        return JSONResponse({"status": "ok", "connected_spokes": [s.name for s in registry.list_connected()]})

    routes.append(Route("/health", endpoint=health, methods=["GET"]))

    app = Starlette(routes=routes)
    app.add_middleware(ExternalBearerAuthMiddleware, expected_token=expected_external_token)
    app.state.registry = registry
    app.state.router = router
    app.state.agent_card = base_card
    return app
