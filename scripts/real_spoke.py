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

from hermes_hub.config import resolve_spoke_name
from hermes_hub.credentials import CredentialUnavailable, require_spoke_credential
from hermes_hub.sessions import SessionMap, SessionStore
from hermes_hub.spoke_client import SpokeClient
from hermes_hub.spoke_executor import SpokeExecutor

logging.basicConfig(level=logging.INFO, format="[real-spoke] %(message)s")


async def main(port: int, spoke_name: str) -> None:
    client_holder: dict = {}

    async def send(frame):
        await client_holder["client"].send(frame)

    session_map = SessionMap(store=SessionStore())
    try:
        expected_credential = require_spoke_credential(spoke_name)
    except CredentialUnavailable as exc:
        logging.error("%s: managed-spoke startup refused: %s", spoke_name, exc)
        raise SystemExit(2) from exc
    logging.info("%s: Keychain credential enforcement enabled", spoke_name)
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
        # The managed hub requires the same Keychain-backed spoke credential
        # at the WebSocket registration boundary; it stays in process memory.
        token=expected_credential,
        skills=[
            {
                "id": "general-reasoning",
                "name": "General reasoning",
                # Task 1.3 / V3 open Q3: a bare id like
                # "Pumpkin::general-reasoning" is an identifier the model
                # cannot judge. Real prose plus an example is what makes
                # "the model suggests a peer" possible.
                "description": (
                    "Runs a full Hermes agent turn on this machine, with "
                    "local filesystem, shell, and network access."
                ),
                "examples": [
                    "Read ~/Git_Repos/finance/normalize.clj and summarize it.",
                    "Can you reach the internal wiki from there?",
                ],
            }
        ],
        on_frame=on_frame,
    )
    client_holder["client"] = client
    await client.run()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    spoke_name = resolve_spoke_name(sys.argv[2] if len(sys.argv) > 2 else "")
    asyncio.run(main(port, spoke_name))
