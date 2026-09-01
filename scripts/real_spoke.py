"""Real spoke process for Gate 4/5 live tests: connects to the hub, and on
each routed task frame runs a REAL Hermes agent turn via
``spoke_executor.run_real_hermes_turn`` (M4), with session continuity via
``SessionMap`` (M5).

Must run under the LIVE Hermes runtime's venv (``run_agent``/``hermes_state``
importable), not this repo's own .venv -- hence the sys.path wiring below
points at both this repo (for hermes_hub) and relies on being invoked with
the Hermes runtime's python interpreter.
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "/Users/mtwomey/Git_Repos/hermes-hub")
sys.path.insert(0, "/Users/mtwomey/.hermes/hermes-agent")

from hermes_hub.credentials import resolve_spoke_credential
from hermes_hub.sessions import SessionMap, SessionStore
from hermes_hub.spoke_client import SpokeClient
from hermes_hub.spoke_executor import SpokeExecutor

logging.basicConfig(level=logging.INFO, format="[real-spoke] %(message)s")


async def main(port: int, spoke_name: str) -> None:
    client_holder: dict = {}

    async def send(frame):
        await client_holder["client"].send(frame)

    session_map = SessionMap(store=SessionStore())
    expected_credential = resolve_spoke_credential(spoke_name)
    logging.info(
        "%s: credential enforcement %s",
        spoke_name,
        "enabled" if expected_credential else "disabled (dev mode)",
    )
    executor = SpokeExecutor(
        spoke_name=spoke_name,
        send=send,
        session_map=session_map,
        expected_credential=expected_credential,
    )

    async def on_frame(frame):
        # Task 2.5: route every frame through the general dispatcher so
        # inbound artifact_begin/chunk/end sequences (ahead of the task
        # frame) get reassembled before the task itself runs.
        await executor.handle_frame(frame)

    client = SpokeClient(
        hub_url=f"ws://127.0.0.1:{port}/hub/v1/spoke",
        name=spoke_name,
        token="",
        skills=[{"id": "general-reasoning", "name": "General reasoning"}],
        on_frame=on_frame,
    )
    client_holder["client"] = client
    await client.run()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    spoke_name = sys.argv[2] if len(sys.argv) > 2 else "Pumpkin"
    asyncio.run(main(port, spoke_name))
