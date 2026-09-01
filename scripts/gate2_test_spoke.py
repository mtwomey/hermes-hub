"""Real spoke process for Gate 2's live test — runs SpokeClient.run() forever."""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "/Users/mtwomey/Git_Repos/hermes-hub")

from hermes_hub.spoke_client import SpokeClient

logging.basicConfig(level=logging.INFO, format="[gate2-spoke] %(message)s")


async def main(port: int) -> None:
    client = SpokeClient(
        hub_url=f"ws://127.0.0.1:{port}/hub/v1/spoke",
        name="Olive",
        token="test-token",
        skills=[{"id": "general-reasoning"}],
        initial_backoff_seconds=1.0,
        max_backoff_seconds=5.0,
    )
    await client.run()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    asyncio.run(main(port))
