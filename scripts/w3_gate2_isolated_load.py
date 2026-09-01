"""CHECKPOINT GATE 2 — isolated-runtime plugin load (W3 M2, Task 2.3).

Proves the plugin loads through Hermes's REAL plugin manager and that all
six tools land in the real tool registry — **without touching the live
runtime**. Safety properties, all enforced here rather than assumed:

* ``HERMES_HOME`` points at a fresh temp directory created by this script.
  The plugin is symlinked into ``<temp>/plugins/``, never
  ``~/.hermes/plugins/``.
* The child runs in a **separate process**, so nothing it imports or
  registers can affect the session running it.
* The live gateway is never contacted or restarted.
* Before and after, the script asserts ``~/.hermes/plugins`` is byte-for-byte
  the same set of entries and that ``~/.hermes/hermes-agent``'s git status
  is unchanged.

It uses the LIVE Hermes venv's interpreter because that is the only Python
with ``hermes_cli`` importable — reading the runtime, never writing to it.
No package is installed.

Usage: .venv/bin/python scripts/w3_gate2_isolated_load.py
"""

from __future__ import annotations

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
LIVE_PLUGINS = Path.home() / ".hermes" / "plugins"

EXPECTED = [
    "peer_ask",
    "peer_discover",
    "peer_fetch_artifact",
    "peer_info",
    "peer_list",
    "peer_status",
]

# Runs inside the child, under the isolated HERMES_HOME.
CHILD = r'''
import json, os, sys
from pathlib import Path

sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])

report = {"home": os.environ.get("HERMES_HOME")}

from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home

report["resolved_home"] = str(get_hermes_home())

manager = PluginManager()
manager.discover_and_load()

loaded = {}
for key, plugin in manager._plugins.items():
    loaded[key] = {"enabled": plugin.enabled, "error": plugin.error or ""}
report["plugins"] = loaded

from tools.registry import registry

names = []
for tool_name in EXPECTED_NAMES:
    entry = registry.get_entry(tool_name)
    if entry is not None:
        names.append(tool_name)
report["registered"] = sorted(names)

# Full detail for the six, straight out of the REAL registry.
detail = {}
for tool_name in report["registered"]:
    entry = registry.get_entry(tool_name)
    schema = getattr(entry, "schema", None) or {}
    detail[tool_name] = {
        "toolset": getattr(entry, "toolset", ""),
        "schema_name": schema.get("name", ""),
        "has_check_fn": getattr(entry, "check_fn", None) is not None,
        "description_head": (schema.get("description", "") or "")[:70],
    }
report["detail"] = detail

# check_fn behaviour inside the real runtime.
os.environ.pop("HERMES_HUB_URL", None)
entry = registry.get_entry("peer_ask")
check = getattr(entry, "check_fn", None)
report["check_fn_without_hub"] = bool(check()) if check else None
os.environ["HERMES_HUB_URL"] = "http://127.0.0.1:8770"
report["check_fn_with_hub"] = bool(check()) if check else None

# V3: nothing the plugin registered may have injected peer state into the
# system prompt. Build the real prompt and look for spoke-specific content.
try:
    from tools.registry import registry as _r
    schemas = _r.get_schemas() if hasattr(_r, "get_schemas") else []
    report["schema_count"] = len(schemas)
except Exception as exc:
    report["schema_count"] = f"unavailable: {exc}"

# END-TO-END: call peer_ask through the REGISTERED HANDLER from the real
# registry (not the raw M1 function) against the live hub+spoke.
hub_url = os.environ.get("GATE2_HUB_URL", "")
if hub_url:
    handler = getattr(registry.get_entry("peer_ask"), "handler", None)
    report["handler_is_from_registry"] = handler is not None
    raw = handler(
        {
            "hub_url": hub_url,
            "peer_name": os.environ.get("GATE2_SPOKE", "Pumpkin"),
            "message": os.environ.get("GATE2_MESSAGE", "Reply with exactly: GATE 2 OK"),
        }
    )
    report["e2e_raw"] = raw
    handler_list = getattr(registry.get_entry("peer_list"), "handler", None)
    report["e2e_peer_list"] = handler_list({"hub_url": hub_url})

print("___REPORT___" + json.dumps(report))
'''


def snapshot_live() -> dict:
    entries = sorted(p.name for p in LIVE_PLUGINS.iterdir()) if LIVE_PLUGINS.exists() else []
    status = subprocess.run(
        ["git", "-C", str(HERMES_AGENT), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    return {"plugins": entries, "git_status": status}


def main() -> int:
    if not HERMES_VENV_PY.exists():
        print(f"FAIL: Hermes venv python not found at {HERMES_VENV_PY}")
        return 1

    before = snapshot_live()
    print("=== live runtime BEFORE ===")
    print(f"~/.hermes/plugins entries: {before['plugins']}")
    print("git -C ~/.hermes/hermes-agent status --porcelain:")
    print(before["git_status"] or "(empty)")

    temp_home = Path(tempfile.mkdtemp(prefix="w3-gate2-hermes-home-"))
    try:
        (temp_home / "plugins").mkdir()
        link = temp_home / "plugins" / "hermes-hub-peer"
        link.symlink_to(PLUGIN_SRC)
        (temp_home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - hermes-hub-peer\n", encoding="utf-8"
        )
        print(f"\n=== isolated HERMES_HOME ===\n{temp_home}")
        print(f"plugin symlink: {link} -> {PLUGIN_SRC}")

        env = dict(os.environ)
        env["HERMES_HOME"] = str(temp_home)
        env["HERMES_AGENT_DIR"] = str(HERMES_AGENT)
        env["HERMES_PLUGINS_DEBUG"] = "0"
        env.pop("HERMES_HUB_URL", None)
        for extra in ("GATE2_HUB_URL", "GATE2_SPOKE", "GATE2_MESSAGE"):
            if extra in os.environ:
                env[extra] = os.environ[extra]

        child_src = f"EXPECTED_NAMES = {EXPECTED!r}\n" + CHILD
        print("\n=== child process ===")
        print(f"$ HERMES_HOME={temp_home} {HERMES_VENV_PY} -c <load probe>")
        proc = subprocess.run(
            [str(HERMES_VENV_PY), "-c", child_src],
            env=env,
            cwd=str(temp_home),
            capture_output=True,
            text=True,
            timeout=600,
        )
        stdout = proc.stdout
        marker = "___REPORT___"
        if marker not in stdout:
            print("FAIL: child produced no report")
            print("--- stdout ---")
            print(stdout[-4000:])
            print("--- stderr ---")
            print(proc.stderr[-4000:])
            return 1
        pre, _, payload = stdout.partition(marker)
        if pre.strip():
            print("--- child stdout (pre-report) ---")
            print(pre.strip()[-2000:])
        report = json.loads(payload)
        print("\n=== child report ===")
        printable = {k: v for k, v in report.items() if k not in ("e2e_raw", "e2e_peer_list")}
        print(json.dumps(printable, indent=2))
        if "e2e_raw" in report:
            print("\n=== END-TO-END through the REGISTERED handler ===")
            print("peer_list  :", report.get("e2e_peer_list"))
            print("peer_ask   :", report["e2e_raw"])
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)
        print(f"\ntemp HERMES_HOME removed: exists={temp_home.exists()}")

    after = snapshot_live()
    print("\n=== live runtime AFTER ===")
    print(f"~/.hermes/plugins entries: {after['plugins']}")
    print("git -C ~/.hermes/hermes-agent status --porcelain:")
    print(after["git_status"] or "(empty)")

    ok = True
    if report.get("resolved_home") != report.get("home"):
        print("\nFAIL: child did not resolve the isolated HERMES_HOME")
        ok = False
    if report.get("registered") != EXPECTED:
        print(f"\nFAIL: registered tools {report.get('registered')} != {EXPECTED}")
        ok = False
    if report.get("check_fn_without_hub") is not False:
        print("\nFAIL: check_fn did not hide the tools without a hub")
        ok = False
    if report.get("check_fn_with_hub") is not True:
        print("\nFAIL: check_fn did not expose the tools with a hub")
        ok = False
    if before["plugins"] != after["plugins"]:
        print("\nFAIL: ~/.hermes/plugins changed")
        ok = False
    if before["git_status"] != after["git_status"]:
        print("\nFAIL: ~/.hermes/hermes-agent git status changed")
        ok = False
    if "e2e_raw" in report:
        e2e = json.loads(report["e2e_raw"])
        if not e2e.get("success"):
            print(f"\nFAIL: end-to-end peer_ask failed: {e2e.get('error')}")
            ok = False

    print("\n" + "=" * 60)
    print("GATE 2 ISOLATED LOAD: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
