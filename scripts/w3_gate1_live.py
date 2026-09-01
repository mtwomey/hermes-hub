"""CHECKPOINT GATE 1 live harness (W3 M1).

Calls each of the six ``peer_*`` handlers DIRECTLY from Python against a
real running hub (``scripts/run_hub.py``) and a real Hermes-agent-backed
spoke (``scripts/real_spoke.py``) — not via curl, not via the CLI.

Also exercises the W1 regression check (wrong credential must fail) and
prints every raw handler output so the recorded evidence can be grepped for
the credential value.

Usage:
    HERMES_HUB_PEER_CREDENTIAL_PUMPKIN=<secret> \\
        .venv/bin/python scripts/w3_gate1_live.py [hub_url] [spoke]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_hub.tools import peer_tools

HUB_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
SPOKE = sys.argv[2] if len(sys.argv) > 2 else "Pumpkin"

results: dict[str, str] = {}


def show(label: str, raw: str) -> dict:
    results[label] = raw
    print(f"\n===== {label} =====")
    parsed = json.loads(raw)
    print(json.dumps(parsed, indent=2)[:2500])
    return parsed


def main() -> int:
    base = {"hub_url": HUB_URL, "peer_name": SPOKE}

    show("peer_list", peer_tools.peer_list({"hub_url": HUB_URL}))
    show("peer_info", peer_tools.peer_info(dict(base)))
    show("peer_discover", peer_tools.peer_discover(dict(base)))

    ask = show(
        "peer_ask",
        peer_tools.peer_ask(
            {
                **base,
                "message": (
                    "In one short sentence, what machine are you running on? "
                    "Then write a file called gate1.txt into your task output "
                    "directory containing exactly the text: W3 GATE 1 LIVE."
                ),
            }
        ),
    )

    show(
        "peer_status",
        peer_tools.peer_status({**base, "task_id": ask.get("task_id", "")}),
    )

    artifacts = ask.get("artifacts") or []
    if artifacts:
        art = artifacts[0]
        dest = Path(tempfile.gettempdir()) / "w3-gate1-fetched.txt"
        fetched = show(
            "peer_fetch_artifact",
            peer_tools.peer_fetch_artifact(
                {
                    **base,
                    "task_id": ask["task_id"],
                    "artifact_id": art["artifact_id"],
                    "output_path": str(dest),
                }
            ),
        )
        if fetched.get("success"):
            print(f"\n--- fetched file contents ({dest}) ---")
            print(dest.read_bytes()[:500])
    else:
        print("\n!!! peer_ask returned no artifacts; peer_fetch_artifact not exercised")

    # -- W1 regression: a WRONG credential must fail -------------------------
    wrong = show(
        "peer_ask_WRONG_CREDENTIAL",
        peer_tools.peer_ask(
            {**base, "message": "this must not run", "credential": "definitely-not-the-secret"}
        ),
    )
    assert wrong["success"] is False, "SECURITY REGRESSION: wrong credential succeeded"

    # -- leak canary ---------------------------------------------------------
    secret = os.environ.get(f"HERMES_HUB_PEER_CREDENTIAL_{SPOKE.upper()}", "")
    combined = "\n".join(results.values())
    print("\n===== leak canary =====")
    if not secret:
        print("NO CREDENTIAL CONFIGURED — canary is vacuous, gate INVALID")
        return 1
    print(f"credential configured : yes ({len(secret)} chars)")
    print(f"occurrences in output : {combined.count(secret)}")
    if secret in combined:
        print("FAIL: credential leaked into tool output")
        return 1
    print("PASS: credential appears in no tool output")

    successes = {k: json.loads(v)["success"] for k, v in results.items()}
    print("\n===== summary =====")
    for k, v in successes.items():
        print(f"  {k}: success={v}")
    expected_ok = [k for k in successes if k != "peer_ask_WRONG_CREDENTIAL"]
    if not all(successes[k] for k in expected_ok):
        print("FAIL: not every handler succeeded")
        return 1
    if len(expected_ok) != 6:
        print(f"FAIL: only {len(expected_ok)} of 6 handlers exercised")
        return 1
    print("PASS: all six handlers succeeded live; wrong credential rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
