"""Mutation-based RED verification for the W3 M1 tests.

Purpose: a test suite that goes green on its first run has proved nothing.
This applies one targeted mutation at a time to the implementation, runs the
test that is supposed to catch it, and asserts the failure is a real
assertion failure (missing behavior) rather than an import/collection error.

Usage: .venv/bin/python scripts/w3_mutation_red.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTEST = [str(REPO / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:warnings"]

TOOLS = REPO / "hermes_hub" / "tools" / "peer_tools.py"
CLIENT = REPO / "hermes_hub" / "hub_client.py"

# (label, file, old, new, test node id, behaviour the mutation removes)
MUTATIONS = [
    (
        "credential-not-attached",
        TOOLS,
        '    credential = resolve_peer_credential(\n        peer_name, explicit=str(args.get("credential") or "")\n    )',
        '    credential = ""',
        "tests/test_peer_tools.py::test_peer_ask_attaches_the_credential_to_the_outbound_task",
        "V5a: peer_ask attaches the per-spoke credential",
    ),
    (
        "credential-env-resolution-removed",
        TOOLS,
        '    env_value = os.environ.get(env_key, "")\n    if env_value:\n        return env_value',
        "    pass",
        "tests/test_peer_tools.py::test_peer_ask_resolves_the_credential_from_the_environment",
        "Task 1.1: env-var credential resolution",
    ),
    (
        "skill-descriptions-dropped",
        TOOLS,
        '                "description": description,\n                "examples": list(skill.get("examples") or []),',
        '                "description": "",\n                "examples": [],',
        "tests/test_peer_tools.py::test_peer_list_surfaces_skill_descriptions_not_just_ids",
        "Task 1.3 / V3 Q3: human skill descriptions in peer_list",
    ),
    (
        "raw-envelope-leaked",
        TOOLS,
        "    return _ok(\n        text=result[\"text\"],\n        task_id=result[\"task_id\"],\n        context_id=result[\"context_id\"],\n        artifacts=result[\"artifacts\"],\n    )",
        '    return _ok(raw_envelope={"jsonrpc": "2.0", "statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}})',
        "tests/test_peer_tools.py::test_peer_ask_output_contains_no_protocol_envelope_keys",
        "Task 1.2: no raw Task/JSON-RPC envelope in model context",
    ),
    (
        "protobuf-state-enum-leaked",
        TOOLS,
        '        state=state[len("TASK_STATE_") :].lower() if state.startswith("TASK_STATE_") else state.lower(),',
        "        state=state,",
        "tests/test_peer_tools.py::test_peer_status_output_contains_no_protocol_envelope_keys",
        "Task 1.2: protobuf state enum stripped from peer_status",
    ),
    (
        "task-scoped-download-guard-removed",
        TOOLS,
        '    if not task_id:\n        return _err(\n            "task_id is required: the hub\'s artifact route is task-scoped "\n            "(/a2a/artifacts/{task_id}/{artifact_id}); use the task_id from "\n            "the peer_ask result that produced this artifact"\n        )',
        "    pass",
        "tests/test_peer_tools.py::test_peer_fetch_artifact_requires_task_id",
        "task-scoped artifact route guard",
    ),
    (
        "sha256-verification-removed",
        CLIENT,
        "        if expected_sha256 and digest != expected_sha256:\n            raise HubClientError(",
        "        if False:\n            raise HubClientError(",
        "tests/test_hub_client.py::test_download_artifact_raises_on_sha256_mismatch",
        "SHA-256 verification on artifact download",
    ),
    (
        "wrong-credential-accepted",
        CLIENT,
        '        if failure is not None:\n            raise HubClientError(failure)',
        "        pass",
        "tests/test_peer_tools.py::test_peer_ask_with_the_wrong_credential_fails",
        "W1 regression: wrong credential must fail the call",
    ),
    (
        "hub-token-not-sent",
        CLIENT,
        '        if self._token:\n            headers["Authorization"] = f"Bearer {self._token}"',
        "        pass",
        "tests/test_peer_tools.py::test_external_token_from_the_environment_is_used",
        "hub external bearer token is sent",
    ),
]


def run(node_id: str) -> tuple[int, str]:
    proc = subprocess.run(
        PYTEST + [node_id], cwd=REPO, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    failures = 0
    for label, path, old, new, node_id, behaviour in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"[SETUP-ERROR] {label}: anchor text not found in {path.name}")
            failures += 1
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            code, output = run(node_id)
        finally:
            path.write_text(original, encoding="utf-8")

        collection_error = ("error" in output.lower() and "1 error" in output) or (
            "ImportError" in output or "SyntaxError" in output or "NameError" in output
        )
        red = code != 0 and "1 failed" in output and not collection_error
        verdict = "RED (assertion)" if red else "NOT-A-VALID-RED"
        if not red:
            failures += 1
        print(f"\n=== {label} ===")
        print(f"behaviour removed : {behaviour}")
        print(f"test              : {node_id}")
        print(f"verdict           : {verdict}")
        tail = [
            line
            for line in output.splitlines()
            if line.startswith("E ") or "failed" in line or "passed" in line
        ]
        for line in tail[:6]:
            print(f"  {line}")

    print("\n" + "=" * 60)
    if failures:
        print(f"MUTATION CHECK FAILED: {failures} mutation(s) not caught")
        return 1
    print(f"ALL {len(MUTATIONS)} MUTATIONS CAUGHT BY A REAL ASSERTION FAILURE")
    # Restoration proof: the suite must be green again.
    proc = subprocess.run(PYTEST + ["tests/"], cwd=REPO, capture_output=True, text=True)
    print("post-restore full suite:", proc.stdout.strip().splitlines()[-1])
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
