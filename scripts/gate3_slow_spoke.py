"""Gate 3 live test: a real hub process + a slow fake spoke.

The spoke here is NOT a full Hermes agent (that's M4's job) -- it's a
deliberately slow task executor that emits several task_status heartbeats
spaced seconds apart, then completes. This isolates Gate 3's requirement
(incremental SSE through the *routing* layer) from M4's requirement (a real
agent turn), matching the plan's own Gate 3 vs Gate 4 split.
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/Users/mtwomey/Git_Repos/hermes-hub")

from hermes_hub.spoke_client import SpokeClient

STEP_SECONDS = 2.0
N_HEARTBEATS = 3


async def on_frame(frame):
    if frame.get("type") != "task":
        return
    task_id = frame["task_id"]
    print(f"[gate3-spoke] received task {task_id!r}: {frame['text']!r}", flush=True)
    for i in range(N_HEARTBEATS):
        await asyncio.sleep(STEP_SECONDS)
        await client.send({"type": "task_status", "task_id": task_id, "state": "working"})
        print(f"[gate3-spoke] sent heartbeat {i}", flush=True)
    await asyncio.sleep(STEP_SECONDS)
    await client.send({"type": "task_complete", "task_id": task_id, "text": "slow answer: 42"})
    print("[gate3-spoke] sent task_complete", flush=True)


async def main(port: int) -> None:
    global client
    client = SpokeClient(
        hub_url=f"ws://127.0.0.1:{port}/hub/v1/spoke",
        name="Olive",
        token="",
        skills=[{"id": "slow-task", "name": "Slow task"}],
        on_frame=on_frame,
    )
    await client.run()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    asyncio.run(main(port))
