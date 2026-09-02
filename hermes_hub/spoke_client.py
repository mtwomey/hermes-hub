"""Spoke-side outbound WebSocket client (H2, H3, H8, H9).

A spoke never binds a listening socket. It connects outbound to the hub at
``ws://<hub>:<port>/hub/v1/spoke``, presents its bearer token in the
``Authorization`` header during the HTTP upgrade (H8), and immediately sends
a JSON registration frame so the hub's registry can pick up its declared
skills without waiting on a separate handshake round-trip.

On any disconnect (H9: sleep/wake, network blip, hub restart) the client
reconnects with exponential backoff capped at ``max_backoff_seconds``,
rather than either giving up or hot-looping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("hermes_hub.spoke_client")

DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0

#: Type for an optional async handler invoked for every non-registration
#: frame received from the hub (used by M4's task execution).
FrameHandler = Callable[[Dict[str, Any]], Awaitable[None]]


def build_registration_frame(
    *, name: str, token: str, skills: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """The frame a spoke sends immediately after connecting."""
    return {
        "type": "register",
        "name": name,
        "token": token,
        "skills": list(skills or []),
    }


class SpokeClient:
    """Outbound WebSocket client run by a spoke process.

    Parameters mirror what a real spoke needs: where the hub is, who this
    spoke is, its bearer token, and the skills it advertises. ``on_frame`` is
    an optional async callback invoked for every frame from the hub that is
    not the connection handshake itself (routed tasks land here in M3/M4).
    """

    def __init__(
        self,
        *,
        hub_url: str,
        name: str,
        token: str,
        skills: Optional[List[Dict[str, Any]]] = None,
        on_frame: Optional[FrameHandler] = None,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        stable_connection_seconds: float = 5.0,
    ) -> None:
        self.hub_url = hub_url
        self.name = name
        self.token = token
        self.skills = list(skills or [])
        self.on_frame = on_frame
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.backoff_multiplier = backoff_multiplier
        #: A connection that stays up at least this long is considered
        #: "stable" and resets the backoff; a connection that dies faster
        #: than this (e.g. immediately after connect) does not, so a hub
        #: that accepts the handshake but then drops every attempt still
        #: produces genuinely increasing backoff instead of resetting to
        #: the floor on every retry.
        self.stable_connection_seconds = stable_connection_seconds

        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._websocket = None
        self.connect_attempts = 0
        self.registration_frames_sent = 0
        self.backoff_delays_used: List[float] = []

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def connect_once(self):
        """Open one connection, send the registration frame, return the socket.

        Split out from :meth:`run` so unit tests can exercise a single
        connect+register cycle without the reconnect loop.
        """
        self.connect_attempts += 1
        websocket = await websockets.connect(
            self.hub_url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            # Hub/spoke traffic is LAN-local; proxy autodiscovery can route an
            # RFC1918 address to an unreachable proxy under launchd.
            proxy=None,
        )
        frame = build_registration_frame(name=self.name, token=self.token, skills=self.skills)
        await websocket.send(json.dumps(frame))
        self.registration_frames_sent += 1
        self._websocket = websocket
        self._connected.set()
        logger.info("spoke %s: connected and registered", self.name)
        return websocket

    async def _receive_loop(self, websocket) -> None:
        async for raw in websocket:
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning("spoke %s: dropped unparseable frame", self.name)
                continue
            if self.on_frame is not None:
                await self.on_frame(frame)

    async def run(self) -> None:
        """Connect, register, and reconnect with backoff forever until stopped."""
        backoff = self.initial_backoff_seconds
        while not self._stop.is_set():
            connected_at = None
            try:
                websocket = await self.connect_once()
                connected_at = time.monotonic()
                try:
                    await self._receive_loop(websocket)
                except ConnectionClosed:
                    pass
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.info("spoke %s: connection attempt failed: %s", self.name, exc)
            finally:
                if connected_at is not None and (
                    time.monotonic() - connected_at >= self.stable_connection_seconds
                ):
                    backoff = self.initial_backoff_seconds
                self._connected.clear()
                self._websocket = None

            if self._stop.is_set():
                break

            self.backoff_delays_used.append(backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * self.backoff_multiplier, self.max_backoff_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._websocket is not None:
            await self._websocket.close()

    async def send(self, frame: Dict[str, Any]) -> None:
        if self._websocket is None:
            raise ConnectionError(f"spoke {self.name} is not connected")
        await self._websocket.send(json.dumps(frame))
