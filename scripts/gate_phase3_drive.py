"""Phase 3 Gate 3 driver (BEA-306). Run with the repo .venv from the worktree.

Scenario A (cancel): peer_ask a slow real-agent task, give up early, then
peer_cancel it mid-run; poll peer_status to CANCELED and keep polling to show
it never flips to completed (no late task_complete).
Scenario B (TTL): run against a hub started with a short GATE_TTL_SECONDS and
do not cancel; the hub must auto-cancel (CANCELED, hermesError=ttl_expired).
Tokens/credential are read from env and never printed.
Usage: gate_phase3_drive.py <port> <gate_dir> cancel|ttl
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from hermes_hub.tools import inflight, peer_tools

PORT = int(sys.argv[1])
GATE_DIR = Path(sys.argv[2])
MODE = sys.argv[3]
HUB = f"http://127.0.0.1:{PORT}"
SPOKE = "GateSpoke"
inflight.store_path = lambda: GATE_DIR / "inflight.json"
peer_tools.PEER_ASK_WAIT_SECONDS = 5.0

BASE = {"hub_url": HUB, "hub_token": os.environ["GATE_EXT_TOKEN"],
        "credential": os.environ["GATE_SPOKE_CRED"], "peer_name": SPOKE}
SLOW = ("Run the shell command `sleep 150 && echo GATE3_SLEEP_DONE` in your terminal "
        "and then tell me exactly what it printed.")


def ts():
    return time.strftime("%H:%M:%S")


def show(label, out):
    keep = {k: out.get(k) for k in ("success", "state", "task_id", "elapsed_s", "error", "hermes_error")}
    if out.get("text"):
        keep["text"] = out["text"][:300]
    print(f"{ts()} {label}: {json.dumps({k: v for k, v in keep.items() if v is not None})}", flush=True)


def raw_task(task_id):
    headers = {"Authorization": "Bearer " + os.environ["GATE_EXT_TOKEN"], "A2A-Version": "1.0"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task_id}}
    t = httpx.post(f"{HUB}/a2a/v1", json=body, headers=headers, timeout=30).json()["result"]
    msg = (t["status"].get("message") or {})
    return t["status"]["state"], (msg.get("metadata") or {}).get("hermesError")


a = json.loads(peer_tools.peer_ask({**BASE, "message": SLOW}, session_id="gate3-session"))
show("peer_ask", a)
tid = a["task_id"]
if MODE == "cancel":
    time.sleep(20)
    show("peer_status before cancel", json.loads(peer_tools.peer_status({**BASE, "task_id": tid})))
    show("peer_cancel", json.loads(peer_tools.peer_cancel({**BASE, "task_id": tid})))
    print(f"{ts()} inflight guard entry after cancel: {inflight.get_inflight('gate3-session', SPOKE)}", flush=True)
    watch = 60
else:
    watch = 200
end = time.time() + watch
last = None
while time.time() < end:
    cur = raw_task(tid)
    if cur != last:
        print(f"{ts()} GetTask {tid}: state={cur[0]} hermesError={cur[1]}", flush=True)
        last = cur
    if MODE == "ttl" and cur[0] == "TASK_STATE_CANCELED":
        # keep watching a little to prove it never flips to completed
        end = min(end, time.time() + 30)
    time.sleep(2)
print(f"{ts()} FINAL GetTask {tid}: state={last[0]} hermesError={last[1]}", flush=True)
