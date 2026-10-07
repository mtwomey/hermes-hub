"""Phase 2 Gate 2 driver (BEA-305). Run with the repo .venv from the worktree.

Uses the real peer_* tool handlers (as Hermes calls them: handler(args,
session_id=...)) and raw HTTP. Tokens/credential are read from env, never
printed. The in-flight store is redirected to the gate dir.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from hermes_hub.tools import inflight, peer_tools

PORT = int(sys.argv[1])
GATE_DIR = Path(sys.argv[2])
HUB = f"http://127.0.0.1:{PORT}"
SPOKE = "GateSpoke"
inflight.store_path = lambda: GATE_DIR / "inflight.json"
peer_tools.PEER_ASK_WAIT_SECONDS = 5.0  # caller "gives up" early so the task is still running

BASE = {"hub_url": HUB, "hub_token": os.environ["GATE_EXT_TOKEN"],
        "credential": os.environ["GATE_SPOKE_CRED"], "peer_name": SPOKE}


def ts():
    return time.strftime("%H:%M:%S")


def show(label, out):
    keep = {k: out.get(k) for k in ("success", "state", "task_id", "context_id",
                                     "original_request", "elapsed_s", "instruction", "error")}
    if out.get("text"):
        keep["text"] = out["text"][:600]
    print(f"{ts()} {label}: {json.dumps({k: v for k, v in keep.items() if v is not None})}", flush=True)


def ask(message, session, **extra):
    return json.loads(peer_tools.peer_ask({**BASE, "message": message, **extra}, session_id=session))


def wait(task_id):
    while True:
        out = json.loads(peer_tools.peer_wait({**BASE, "task_id": task_id, "seconds": 120}))
        if out.get("state") != "working":
            return out


SLOW = ("Run the shell command `sleep 20 && hostname -s` in your terminal and "
        "tell me the short hostname it printed.")
REPHRASED = ("What's this machine's short hostname? Please get it by running "
             "`sleep 20 && hostname -s` in the shell.")

print(f"{ts()} === Scenario A: rephrased double ask from one session ===", flush=True)
a1 = ask(SLOW, "gate-session-1")
show("A1 peer_ask", a1)
a2 = ask(REPHRASED, "gate-session-1")
show("A2 peer_ask (rephrased, same session)", a2)
a_final = wait(a1["task_id"])
show("A1 peer_wait", a_final)

print(f"{ts()} === Scenario B: forced new_request=true rephrased duplicate ===", flush=True)
b1 = ask("Could you tell me the hostname of this computer again? Use `sleep 20 && hostname -s`.",
         "gate-session-1", new_request=True)
show("B1 peer_ask new_request=true", b1)
b_final = wait(b1["task_id"]) if b1.get("state") == "working" else b1
show("B1 peer_wait", b_final)
print(f"{ts()} B earlier task id = {a1['task_id']}; referenced in answer: "
      f"{a1['task_id'] in (b_final.get('text') or '')}", flush=True)

print(f"{ts()} === Scenario C: duplicate messageId via raw HTTP ===", flush=True)
mid = f"gate-dup-{uuid.uuid4().hex}"
headers = {"Authorization": "Bearer " + os.environ["GATE_EXT_TOKEN"], "A2A-Version": "1.0"}
ids = []
for i in range(2):
    body = {"jsonrpc": "2.0", "id": i, "method": "SendMessage", "params": {
        "configuration": {"returnImmediately": True},
        "message": {"role": "ROLE_USER", "messageId": mid,
                    "parts": [{"text": "Reply with exactly the word PONG." if i == 0 else "Say PONG please (retry)."}],
                    "metadata": {"targetSpoke": SPOKE, "spokeCredential": os.environ["GATE_SPOKE_CRED"]}}}}
    r = httpx.post(f"{HUB}/a2a/v1", json=body, headers=headers, timeout=30).json()
    task = r["result"].get("task") or r["result"]
    ids.append(task["id"])
    print(f"{ts()} C send #{i + 1} messageId={mid} -> task {task['id']} state {task['status']['state']}", flush=True)
print(f"{ts()} C same task id: {ids[0] == ids[1]}", flush=True)
show("C peer_wait", wait(ids[0]))
