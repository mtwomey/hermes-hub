"""Minimal standalone WebSocket echo/log server for Gate 2's live test.

This is NOT the real hub server (that's M3's hermes_hub/hub_server.py). It
exists only to prove SpokeClient's live connect/register/disconnect/
reconnect behavior against a real separate process, before the hub's own
routing logic exists.
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets


async def handler(websocket):
    peer = websocket.remote_address
    print(f"[gate2-hub] connection opened from {peer}", flush=True)
    try:
        async for raw in websocket:
            frame = json.loads(raw)
            if frame.get("type") == "register":
                print(
                    f"[gate2-hub] REGISTERED spoke name={frame.get('name')!r} "
                    f"skills={frame.get('skills')!r}",
                    flush=True,
                )
            else:
                print(f"[gate2-hub] frame: {frame}", flush=True)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print(f"[gate2-hub] DISCONNECT from {peer}", flush=True)


async def main(port: int) -> None:
    async with websockets.serve(handler, "127.0.0.1", port):
        print(f"[gate2-hub] listening on 127.0.0.1:{port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    asyncio.run(main(port))
