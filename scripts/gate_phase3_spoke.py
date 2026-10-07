"""Phase 3 Gate 3 (BEA-306): a REAL Hermes-agent spoke on a throwaway hub.

Same construction as scripts/real_spoke.py (bootstrap, run_real_hermes_turn,
RequestLedger) but credentials come from per-run throwaway env values, the
session map and ledger live in a temp dir, and every agent execution is logged
as ``AGENT_EXEC``/``AGENT_RETURNED`` so the gate can time how fast a cancelled
turn actually stopped. ``on_agent`` is passed through so task_cancel reaches
the live agent via request_hard_interrupt.
Run under the Hermes store interpreter (see services/hermes-spoke-wrapper.sh).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

_agent_root = os.environ.get("HERMES_AGENT_ROOT") or str(Path.home() / ".hermes" / "hermes-agent")
if _agent_root not in sys.path:
    sys.path.insert(0, _agent_root)

import hermes_bootstrap  # noqa: F401,E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_hub.ledger import RequestLedger  # noqa: E402
from hermes_hub.sessions import SessionMap, SessionStore  # noqa: E402
from hermes_hub.spoke_client import SpokeClient  # noqa: E402
from hermes_hub.spoke_executor import SpokeExecutor, build_spoke_prompt, run_real_hermes_turn  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[gate-spoke] %(asctime)s %(name)s %(message)s")
log = logging.getLogger("gate-spoke")


def counting_runner(*, text, session_id, task_id, context_id, spoke_name,
                    output_dir=None, input_files=None, recent_requests=None, on_agent=None):
    import time as _t
    t0 = _t.monotonic()
    log.info("AGENT_EXEC task=%s recent=%s text=%r", task_id,
             [e["task_id"] for e in (recent_requests or [])], text[:80])
    answer = run_real_hermes_turn(text=text, session_id=session_id, task_id=task_id,
                                  context_id=context_id, spoke_name=spoke_name,
                                  output_dir=output_dir, input_files=input_files,
                                  recent_requests=recent_requests, on_agent=on_agent)
    log.info("AGENT_RETURNED task=%s after=%.1fs answer=%r", task_id, _t.monotonic() - t0, answer[:200])
    return answer


async def main(port: int, name: str, workdir: Path) -> None:
    holder: dict = {}

    async def send(frame):
        await holder["client"].send(frame)

    executor = SpokeExecutor(
        spoke_name=name,
        send=send,
        session_map=SessionMap(store=SessionStore(workdir / "sessions.db")),
        agent_runner=counting_runner,
        expected_credential=os.environ["GATE_SPOKE_CRED"],
        artifact_root=workdir / "art",
        ledger=RequestLedger(workdir / "ledger.db"),
    )

    async def on_frame(frame):
        await executor.handle_frame(frame)

    client = SpokeClient(
        hub_url=f"ws://127.0.0.1:{port}/hub/v1/spoke",
        name=name,
        token=os.environ["GATE_JOIN_TOKEN"],
        skills=[{"id": "general-reasoning", "name": "General reasoning",
                 "description": "Gate 3 test spoke (real Hermes agent turn)."}],
        on_frame=on_frame,
    )
    holder["client"] = client
    await client.run()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
