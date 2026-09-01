"""Tests for :mod:`hermes_hub.hub_client` — the thin HTTP client the
``peer_*`` tools use to reach a running hub's A2A surface (W3 M1).

Ported in shape from hermes-peer's ``client.py`` (V14) but pointed at the
hub, and driven against a REAL uvicorn-hosted hub app (see
``tests/hub_harness.py``) rather than a mocked transport.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from hermes_hub.hub_client import HubClient, HubClientError
from hub_harness import LiveHub


def run(coro):
    return asyncio.run(coro)


def test_agent_card_lists_connected_spokes_and_skill_descriptions():
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "skills": [
                    {
                        "id": "general-reasoning",
                        "name": "General reasoning",
                        "description": "Answers questions using the local Hermes agent.",
                        "examples": ["What can you reach from here?"],
                    }
                ],
            }
        ]
    ) as hub:
        client = HubClient(hub_url=hub.base_url)
        card = run(client.agent_card())
        skill_ids = [s["id"] for s in card["skills"]]
        assert "Olive::general-reasoning" in skill_ids
        descriptions = " ".join(s.get("description", "") for s in card["skills"])
        assert "Answers questions using the local Hermes agent." in descriptions


def test_ask_returns_final_text_task_id_and_context_id():
    with LiveHub(spokes=[{"name": "Olive", "reply": "forty-two"}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "what is 6*7?"))
        assert result["text"] == "forty-two"
        assert result["task_id"]
        assert result["context_id"]


def test_ask_sends_credential_as_spoke_credential_metadata():
    """V5a: the caller's per-spoke credential must reach the spoke in the
    routed task frame's ``credential`` field."""
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": "s3cr3t"}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "hello", credential="s3cr3t"))
        assert result["text"] == "ok"
        task_frames = [f for f in hub.connections["Olive"].received if f["type"] == "task"]
        assert task_frames[0]["credential"] == "s3cr3t"


def test_ask_with_wrong_credential_fails():
    """W1 regression: a wrong credential must be rejected by the spoke."""
    with LiveHub(spokes=[{"name": "Olive", "expected_credential": "s3cr3t"}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        with pytest.raises(HubClientError) as exc:
            run(client.ask("Olive", "hello", credential="WRONG"))
        assert "unauthorized" in str(exc.value).lower()


def test_ask_reports_artifacts_with_sha256_and_url(tmp_path):
    payload = b"artifact bytes from the spoke"
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "artifact": {"data": payload, "name": "note.txt", "mime_type": "text/plain"},
            }
        ],
        artifact_root=tmp_path,
    ) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "make a file"))
        assert len(result["artifacts"]) == 1
        art = result["artifacts"][0]
        assert art["name"] == "note.txt"
        assert art["sha256"] == hashlib.sha256(payload).hexdigest()


def test_get_task_returns_a_task_for_a_completed_ask():
    with LiveHub(spokes=[{"name": "Olive", "reply": "done"}]) as hub:
        client = HubClient(hub_url=hub.base_url)
        asked = run(client.ask("Olive", "go"))
        task = run(client.get_task(asked["task_id"]))
        assert task.get("id") == asked["task_id"]


def test_inline_artifact_is_also_downloadable(tmp_path):
    """Small artifacts arrive inline (under 64KB) rather than chunked. They
    must still be fetchable by task_id/artifact_id, otherwise
    ``peer_fetch_artifact`` works for large files and 404s for small ones."""
    payload = b"a small inline artifact"
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "artifact": {"data": payload, "name": "small.txt", "mime_type": "text/plain"},
            }
        ],
        artifact_root=tmp_path,
    ) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "make a small file"))
        art = result["artifacts"][0]
        assert art["sha256"] == hashlib.sha256(payload).hexdigest()
        dest = tmp_path / "fetched-small.txt"
        path = run(
            client.download_artifact(
                art["task_id"],
                art["artifact_id"],
                dest,
                expected_sha256=art["sha256"],
            )
        )
        assert path.read_bytes() == payload


def test_download_artifact_verifies_sha256_and_writes_file(tmp_path):
    payload = b"downloadable artifact payload"
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "artifact": {"data": payload, "name": "blob.bin", "chunked": True},
            }
        ],
        artifact_root=tmp_path,
    ) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "make a file"))
        art = result["artifacts"][0]
        dest = tmp_path / "out.bin"
        path = run(
            client.download_artifact(
                art["task_id"],
                art["artifact_id"],
                dest,
                expected_sha256=art["sha256"],
            )
        )
        assert path.read_bytes() == payload


def test_download_artifact_raises_on_sha256_mismatch(tmp_path):
    payload = b"payload"
    with LiveHub(
        spokes=[
            {
                "name": "Olive",
                "artifact": {"data": payload, "name": "b.bin", "chunked": True},
            }
        ],
        artifact_root=tmp_path,
    ) as hub:
        client = HubClient(hub_url=hub.base_url)
        result = run(client.ask("Olive", "make a file"))
        art = result["artifacts"][0]
        with pytest.raises(HubClientError) as exc:
            run(
                client.download_artifact(
                    art["task_id"],
                    art["artifact_id"],
                    tmp_path / "out.bin",
                    expected_sha256="0" * 64,
                )
            )
        assert "sha-256" in str(exc.value).lower() or "sha256" in str(exc.value).lower()


def test_external_bearer_token_is_sent_when_configured():
    with LiveHub(spokes=[{"name": "Olive"}], external_token="ext-token") as hub:
        unauthenticated = HubClient(hub_url=hub.base_url)
        with pytest.raises(HubClientError):
            run(unauthenticated.agent_card())
        authenticated = HubClient(hub_url=hub.base_url, token="ext-token")
        card = run(authenticated.agent_card())
        assert card["name"] == "hermes-hub"


def test_unreachable_hub_raises_hub_client_error():
    client = HubClient(hub_url="http://127.0.0.1:1")
    with pytest.raises(HubClientError):
        run(client.agent_card())
