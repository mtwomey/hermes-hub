"""V3 cache-safety proof for the hermes-hub-peer plugin (W3, Gate 2).

The hard stop in the plan: "Cache-safe discovery (V3) appears to require
injecting live peer state into the system prompt. It must not — a short
static line is fine, per-spoke content is not."

Inspecting the plugin source shows it never calls a prompt surface. That is
necessary but not sufficient: a tool's *schema description* also travels in
every API call, and Hermes builds the system prompt from real machinery.
This script proves the property empirically:

1. Load the plugin in an isolated ``HERMES_HOME`` (temp dir, subprocess)
   with a live hub + spoke connected and reachable.
2. Build the REAL system prompt via ``AIAgent._build_system_prompt()``.
3. Assert the connected spoke's name and its skill text appear NOWHERE in
   the system prompt, and that the prompt is byte-identical whether or not
   a spoke is connected.

Step 3's second half is the decisive one: if a spoke connecting changed the
prompt by even one byte, every open conversation's cache would be
invalidated on connect/disconnect.

Usage:
    GATE2_HUB_URL=http://127.0.0.1:8770 \
    .venv/bin/python scripts/w3_gate2_cache_safety.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_SRC = REPO / "plugin" / "hermes_hub_peer"
HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
HERMES_VENV_PY = HERMES_AGENT / "venv" / "bin" / "python3"

CHILD = r'''
import hashlib, json, os, sys
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])

from hermes_cli.plugins import PluginManager
PluginManager().discover_and_load()

from tools.registry import registry
report = {"registered": sorted(
    n for n in ["peer_ask","peer_list","peer_info","peer_discover","peer_status","peer_fetch_artifact"]
    if registry.get_entry(n) is not None
)}

# Prove the tools are LIVE in this process (check_fn true) — otherwise a
# clean prompt would be a vacuous pass.
entry = registry.get_entry("peer_ask")
report["check_fn"] = bool(entry.check_fn()) if entry.check_fn else None

# Confirm the hub really has the spoke connected right now, so "spoke name
# absent from the prompt" is a meaningful statement.
import urllib.request
try:
    with urllib.request.urlopen(os.environ["GATE2_HUB_URL"] + "/health", timeout=10) as r:
        report["hub_health"] = json.loads(r.read())
except Exception as exc:
    report["hub_health"] = f"error: {exc}"

from run_agent import AIAgent
agent = AIAgent(quiet_mode=True, skip_memory=True, skip_context_files=True, platform="cli")
prompt = agent._build_system_prompt()
report["prompt_len"] = len(prompt)
report["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()

needles = {
    "spoke_name": os.environ.get("GATE2_SPOKE", "Pumpkin"),
    "skill_id": "general-reasoning",
    "skill_description": "Runs a full Hermes agent turn on this machine",
    "namespaced_skill": "::general-reasoning",
    "card_phrase": "Currently connected",
    "hub_url": os.environ["GATE2_HUB_URL"],
}
report["needles_in_prompt"] = {k: (v in prompt) for k, v in needles.items()}
report["prompt_text"] = prompt

# Static mentions of the tools themselves are fine and expected somewhere in
# the tool schemas; record whether the *prompt* mentions them at all.
report["prompt_mentions_peer_ask"] = "peer_ask" in prompt

print("___REPORT___" + json.dumps(report))
'''


def run_child(hub_url: str, spoke: str, home: Path | None = None) -> dict:
    """Load the plugin under an isolated HERMES_HOME and build the real prompt.

    ``home`` may be supplied so two probes share the SAME home path. That
    matters: Hermes embeds the profile path in the system prompt, so two
    different temp dirs produce two different prompt hashes for reasons that
    have nothing to do with peer state. Comparing connect vs disconnect
    requires holding the home constant.
    """
    temp_home = Path(home) if home is not None else Path(
        tempfile.mkdtemp(prefix="w3-gate2-cache-")
    )
    owns = home is None
    try:
        temp_home.mkdir(parents=True, exist_ok=True)
        plugins = temp_home / "plugins"
        plugins.mkdir(exist_ok=True)
        link = plugins / "hermes-hub-peer"
        if not link.exists():
            link.symlink_to(PLUGIN_SRC)
        (temp_home / "config.yaml").write_text(
            # A minimal provider config so AIAgent can construct and build a
            # real system prompt. No API key is present and no API call is
            # made — the prompt is built locally.
            "model:\n"
            "  default: claude-opus-5\n"
            "  provider: anthropic\n"
            "plugins:\n  enabled:\n    - hermes-hub-peer\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["HERMES_HOME"] = str(temp_home)
        env["HERMES_AGENT_DIR"] = str(HERMES_AGENT)
        env["HERMES_HUB_URL"] = hub_url
        env["GATE2_HUB_URL"] = hub_url
        env["GATE2_SPOKE"] = spoke
        proc = subprocess.run(
            [str(HERMES_VENV_PY), "-c", CHILD],
            env=env,
            cwd=str(temp_home),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if "___REPORT___" not in proc.stdout:
            print("--- child stdout ---")
            print(proc.stdout[-3000:])
            print("--- child stderr ---")
            print(proc.stderr[-3000:])
            raise SystemExit("child produced no report")
        return json.loads(proc.stdout.partition("___REPORT___")[2])
    finally:
        if owns:
            shutil.rmtree(temp_home, ignore_errors=True)


def main() -> int:
    hub_url = os.environ.get("GATE2_HUB_URL", "http://127.0.0.1:8770")
    spoke = os.environ.get("GATE2_SPOKE", "Pumpkin")
    #: When set, the spoke is expected to be DISCONNECTED for this run; the
    #: recorded prompt hash is then compared against the connected run's.
    expect_disconnected = os.environ.get("GATE2_EXPECT_DISCONNECTED", "") == "1"
    baseline_hash = os.environ.get("GATE2_BASELINE_PROMPT_SHA256", "")

    label = "spoke DISCONNECTED" if expect_disconnected else "spoke CONNECTED"
    print(f"=== probe: {label} ===")
    home_override = os.environ.get("GATE2_FIXED_HOME", "")
    a = run_child(hub_url, spoke, Path(home_override) if home_override else None)
    a.pop("prompt_text", None)
    print(json.dumps(a, indent=2))

    ok = True
    if a.get("check_fn") is not True:
        print("\nFAIL: peer tools were not live; a clean prompt proves nothing")
        ok = False
    health = a.get("hub_health")
    connected = (health or {}).get("connected_spokes") if isinstance(health, dict) else None
    if expect_disconnected:
        if connected is None or spoke in connected:
            print(f"\nFAIL: expected {spoke} disconnected, hub reports {health}")
            ok = False
    else:
        if not isinstance(health, dict) or spoke not in (connected or []):
            print(f"\nFAIL: spoke {spoke} was not connected: {health}")
            ok = False
    leaked = [k for k, v in (a.get("needles_in_prompt") or {}).items() if v]
    if leaked:
        print(f"\nFAIL: live peer state found in the system prompt: {leaked}")
        ok = False
    if baseline_hash:
        same = a.get("prompt_sha256") == baseline_hash
        print(
            f"\nprompt hash vs baseline: "
            f"{'IDENTICAL' if same else 'CHANGED'}\n"
            f"  baseline : {baseline_hash}\n"
            f"  this run : {a.get('prompt_sha256')}"
        )
        if not same:
            print(
                "FAIL: the system prompt changed with spoke connectivity — "
                "that would invalidate every open conversation's cache"
            )
            ok = False

    print("\n" + "=" * 60)
    print(f"V3 CACHE SAFETY ({label}): " + ("PASS" if ok else "FAIL"))
    if ok:
        print(
            f"  system prompt sha256 : {a['prompt_sha256']}\n"
            f"  length               : {a['prompt_len']} chars\n"
            f"  hub reports connected: {connected}\n"
            f"  peer state in prompt : none of {sorted(a['needles_in_prompt'])}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
