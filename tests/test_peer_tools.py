"""Tests for the six ``peer_*`` model tools (W3 M1).

Names and schema shapes are ported from hermes-peer's
``hermes_peer/tools/peer_tool.py`` (V14). What changes here is the target:
one hub instead of N peers, and the addition of a per-spoke credential (V5a)
that the caller attaches and the spoke checks.

These tests exercise the handlers against a REAL uvicorn-hosted hub
(``tests/hub_harness.py``), not a mock. ``pytest-asyncio`` is deliberately
not used — the handlers are synchronous and drive their own event loop.
"""

from __future__ import annotations

import json

import pytest

from hermes_hub.tools import peer_tools
from hub_harness import LiveHub

SECRET = "canary-credential-value-do-not-leak"


def call(handler, hub, **args):
    """Invoke a tool handler pointed at ``hub``, returning the parsed JSON."""
    args.setdefault("hub_url", hub.base_url)
    return json.loads(handler(args))


# -- schemas ----------------------------------------------------------------


def test_all_six_tool_names_are_exported_with_schemas():
    names = [spec.name for spec in peer_tools.TOOL_SPECS]
    assert names == [
        "peer_list",
        "peer_info",
        "peer_discover",
        "peer_ask",
        "peer_status",
        "peer_fetch_artifact",
    ]
    for spec in peer_tools.TOOL_SPECS:
        assert spec.schema["name"] == spec.name
        assert spec.schema["description"]
        assert spec.schema["parameters"]["type"] == "object"
        assert callable(spec.handler)


def test_every_handler_returns_a_json_string():
    for spec in peer_tools.TOOL_SPECS:
        out = spec.handler({"hub_url": "http://127.0.0.1:1"})
        assert isinstance(out, str)
        parsed = json.loads(out)
        assert parsed["success"] is False
        assert parsed["error"]


# -- peer_list --------------------------------------------------------------


def test_peer_list_reports_connected_spokes():
    with LiveHub(spokes=[{"name": "Olive"}, {"name": "Pumpkin"}]) as hub:
        out = call(peer_tools.peer_list, hub)
        assert out["success"] is True
        assert sorted(p["name"] for p in out["peers"]) == ["Olive", "Pumpkin"]


def test_peer_list_reports_no_spokes_cleanly():
    with LiveHub() as hub:
        out = call(peer_tools.peer_list, hub)
        assert out["success"] is True
        assert out["peers"] == []


def test_peer_list_surfaces_skill_descriptions_not_just_ids():
    """Task 1.3 / V3 open Q3: ``Olive::general-reasoning`` is an identifier a
    model cannot judge. The human description and examples must be present so
    the model can actually suggest a peer."""
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "skills": [
                    {
                        "id": "dremio-access",
                        "name": "Dremio access",
                        "description": "Can query the corporate Dremio warehouse.",
                        "examples": ["What tables are in the sales schema?"],
                    }
                ],
            }
        ]
    ) as hub:
        out = call(peer_tools.peer_list, hub)
        blob = json.dumps(out)
        assert "Can query the corporate Dremio warehouse." in blob
        assert "What tables are in the sales schema?" in blob


# -- peer_info --------------------------------------------------------------


def test_peer_info_returns_one_spokes_identity_and_skills():
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "skills": [
                    {
                        "id": "dremio-access",
                        "name": "Dremio access",
                        "description": "Can query the corporate Dremio warehouse.",
                    }
                ],
            },
            {"name": "Pumpkin", "skills": [{"id": "local", "name": "Local"}]},
        ]
    ) as hub:
        out = call(peer_tools.peer_info, hub, peer_name="Olive")
        assert out["success"] is True
        assert out["peer"]["name"] == "Olive"
        skill_ids = [s["id"] for s in out["peer"]["skills"]]
        assert "dremio-access" in skill_ids
        # Only Olive's skills, not Pumpkin's.
        assert "local" not in skill_ids
        assert (
            out["peer"]["skills"][0]["description"]
            == "Can query the corporate Dremio warehouse."
        )


def test_peer_info_for_unknown_spoke_reports_not_connected():
    with LiveHub(spokes=[{"name": "Olive"}]) as hub:
        out = call(peer_tools.peer_info, hub, peer_name="Nonexistent")
        assert out["success"] is False
        assert "not" in out["error"].lower()


# -- peer_discover ----------------------------------------------------------


def test_peer_discover_refreshes_and_caches_a_spokes_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(peer_tools, "_cache_path", lambda: tmp_path / "peers.json")
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "skills": [
                    {
                        "id": "dremio-access",
                        "name": "Dremio access",
                        "description": "Can query the corporate Dremio warehouse.",
                    }
                ],
            }
        ]
    ) as hub:
        out = call(peer_tools.peer_discover, hub, peer_name="Olive")
        assert out["success"] is True
        assert [s["id"] for s in out["skills"]] == ["dremio-access"]

    cached = json.loads((tmp_path / "peers.json").read_text())
    assert "Olive" in cached
    assert cached["Olive"]["skills"][0]["id"] == "dremio-access"


def test_peer_discover_cache_never_contains_a_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(peer_tools, "_cache_path", lambda: tmp_path / "peers.json")
    monkeypatch.setenv("HERMES_HUB_PEER_CREDENTIAL_OLIVE", SECRET)
    with LiveHub(spokes=[{"name": "Olive"}]) as hub:
        call(peer_tools.peer_discover, hub, peer_name="Olive")
    assert SECRET not in (tmp_path / "peers.json").read_text()


# -- peer_ask ---------------------------------------------------------------


def test_peer_ask_returns_the_spokes_reply():
    with LiveHub(spokes=[{"name": "Olive", "reply": "yes, I can reach Dremio"}]) as hub:
        out = call(peer_tools.peer_ask, hub, peer_name="Olive", message="can you reach Dremio?")
        assert out["success"] is True
        assert out["text"] == "yes, I can reach Dremio"
        assert out["task_id"]
        assert out["context_id"]


def test_peer_ask_output_contains_no_protocol_envelope_keys():
    """Task 1.2: never dump raw Task JSON into the model's context."""
    with LiveHub(spokes=[{"name": "Olive", "reply": "hi"}]) as hub:
        raw = peer_tools.peer_ask(
            {"hub_url": hub.base_url, "peer_name": "Olive", "message": "hello"}
        )
    for forbidden in ("jsonrpc", "statusUpdate", "TASK_STATE_", "protobuf"):
        assert forbidden not in raw


def test_peer_ask_attaches_the_credential_to_the_outbound_task():
    """Task 1.1 / V5a: the per-spoke credential must reach the spoke."""
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": SECRET}]) as hub:
        out = call(
            peer_tools.peer_ask,
            hub,
            peer_name="Olive",
            message="hi",
            credential=SECRET,
        )
        assert out["success"] is True
        task_frames = [f for f in hub.connections["Olive"].received if f["type"] == "task"]
        assert task_frames[0]["credential"] == SECRET


def test_peer_ask_resolves_the_credential_from_the_environment(monkeypatch):
    """Resolution order mirrors credentials.py: explicit arg -> env
    ``HERMES_HUB_PEER_CREDENTIAL_<SPOKE>`` -> Keychain -> empty (dev)."""
    monkeypatch.setenv("HERMES_HUB_PEER_CREDENTIAL_OLIVE", SECRET)
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": SECRET}]) as hub:
        out = call(peer_tools.peer_ask, hub, peer_name="Olive", message="hi")
        assert out["success"] is True
        task_frames = [f for f in hub.connections["Olive"].received if f["type"] == "task"]
        assert task_frames[0]["credential"] == SECRET


def test_explicit_credential_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("HERMES_HUB_PEER_CREDENTIAL_OLIVE", "env-value")
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": SECRET}]) as hub:
        out = call(
            peer_tools.peer_ask,
            hub,
            peer_name="Olive",
            message="hi",
            credential=SECRET,
        )
        assert out["success"] is True


def test_peer_ask_with_the_wrong_credential_fails(monkeypatch):
    """W1 regression check: a wrong credential must not execute."""
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": SECRET}]) as hub:
        out = call(
            peer_tools.peer_ask,
            hub,
            peer_name="Olive",
            message="hi",
            credential="definitely-wrong",
        )
        assert out["success"] is False
        assert "unauthorized" in out["error"].lower()


def test_no_tool_output_ever_contains_the_credential(monkeypatch, tmp_path):
    """Leak canary, same discipline as W1's: run every tool with a credential
    configured and assert the secret appears in no output."""
    monkeypatch.setattr(peer_tools, "_cache_path", lambda: tmp_path / "peers.json")
    monkeypatch.setenv("HERMES_HUB_PEER_CREDENTIAL_OLIVE", SECRET)
    payload = b"artifact bytes"
    outputs = []
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "expected_credential": SECRET,
                "artifact": {"data": payload, "name": "a.bin", "chunked": True},
            }
        ],
        artifact_root=tmp_path / "artifacts",
    ) as hub:
        base = {"hub_url": hub.base_url, "peer_name": "Olive"}
        outputs.append(peer_tools.peer_list({"hub_url": hub.base_url}))
        outputs.append(peer_tools.peer_info(dict(base)))
        outputs.append(peer_tools.peer_discover(dict(base)))
        ask_raw = peer_tools.peer_ask({**base, "message": "make a file"})
        outputs.append(ask_raw)
        ask = json.loads(ask_raw)
        outputs.append(peer_tools.peer_status({**base, "task_id": ask["task_id"]}))
        art = ask["artifacts"][0]
        outputs.append(
            peer_tools.peer_fetch_artifact(
                {
                    **base,
                    "task_id": ask["task_id"],
                    "artifact_id": art["artifact_id"],
                    "output_path": str(tmp_path / "fetched.bin"),
                }
            )
        )
    combined = "\n".join(outputs)
    assert SECRET not in combined
    # ...and every call actually succeeded, so this is not a vacuous pass.
    assert all(json.loads(o)["success"] is True for o in outputs)


# -- peer_status ------------------------------------------------------------


def test_peer_status_reads_a_task_by_id():
    with LiveHub(spokes=[{"name": "Olive", "reply": "done"}]) as hub:
        ask = call(peer_tools.peer_ask, hub, peer_name="Olive", message="go")
        out = call(peer_tools.peer_status, hub, peer_name="Olive", task_id=ask["task_id"])
        assert out["success"] is True
        assert out["task_id"] == ask["task_id"]
        assert out["state"]


def test_peer_status_output_contains_no_protocol_envelope_keys():
    with LiveHub(spokes=[{"name": "Olive", "reply": "done"}]) as hub:
        ask = call(peer_tools.peer_ask, hub, peer_name="Olive", message="go")
        raw = peer_tools.peer_status(
            {"hub_url": hub.base_url, "peer_name": "Olive", "task_id": ask["task_id"]}
        )
    for forbidden in ("jsonrpc", "statusUpdate", "TASK_STATE_"):
        assert forbidden not in raw
    assert json.loads(raw)["state"] == "completed"


def test_peer_status_for_unknown_task_reports_failure():
    with LiveHub(spokes=[{"name": "Olive"}]) as hub:
        out = call(peer_tools.peer_status, hub, peer_name="Olive", task_id="no-such-task")
        assert out["success"] is False


# -- peer_fetch_artifact ----------------------------------------------------


def test_peer_fetch_artifact_downloads_and_verifies(tmp_path):
    payload = b"the real artifact bytes, verified by sha-256"
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "artifact": {"data": payload, "name": "note.txt", "chunked": True},
            }
        ],
        artifact_root=tmp_path / "artifacts",
    ) as hub:
        ask = call(peer_tools.peer_ask, hub, peer_name="Olive", message="write a file")
        art = ask["artifacts"][0]
        dest = tmp_path / "fetched.txt"
        out = call(
            peer_tools.peer_fetch_artifact,
            hub,
            peer_name="Olive",
            task_id=ask["task_id"],
            artifact_id=art["artifact_id"],
            output_path=str(dest),
        )
        assert out["success"] is True
        assert out["path"] == str(dest)
        assert out["sha256"] == art["sha256"]
        assert dest.read_bytes() == payload


def test_peer_fetch_artifact_requires_task_id(tmp_path):
    """The hub's download route is task-scoped; omitting task_id is a 404,
    so the tool must say so clearly rather than surfacing a raw 404."""
    with LiveHub(spokes=[{"name": "Olive"}]) as hub:
        out = call(
            peer_tools.peer_fetch_artifact,
            hub,
            peer_name="Olive",
            artifact_id="art1",
            output_path=str(tmp_path / "x.bin"),
        )
        assert out["success"] is False
        assert "task_id" in out["error"]


# -- config surface ---------------------------------------------------------


def test_hub_url_falls_back_to_the_environment(monkeypatch):
    with LiveHub(spokes=[{"name": "Olive"}]) as hub:
        monkeypatch.setenv("HERMES_HUB_URL", hub.base_url)
        out = json.loads(peer_tools.peer_list({}))
        assert out["success"] is True
        assert [p["name"] for p in out["peers"]] == ["Olive"]


def test_external_token_from_the_environment_is_used(monkeypatch):
    with LiveHub(spokes=[{"name": "Olive"}], external_token="ext-token") as hub:
        monkeypatch.setenv("HERMES_HUB_URL", hub.base_url)
        assert json.loads(peer_tools.peer_list({}))["success"] is False
        monkeypatch.setenv("HERMES_HUB_TOKEN", "ext-token")
        assert json.loads(peer_tools.peer_list({}))["success"] is True


def test_tools_are_hidden_when_no_hub_is_configured(monkeypatch):
    """check_fn (Task 2.1), mirroring hermes-peer's ``_enabled`` pattern."""
    monkeypatch.delenv("HERMES_HUB_URL", raising=False)
    monkeypatch.setattr(peer_tools, "_configured_hub_url_from_config", lambda: "")
    assert peer_tools.hub_configured() is False
    monkeypatch.setenv("HERMES_HUB_URL", "http://127.0.0.1:8770")
    assert peer_tools.hub_configured() is True
