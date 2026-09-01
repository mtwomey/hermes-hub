"""Tests for hermes_hub.artifacts: storage, SHA-256, inline/URL threshold
(Task 2.1, ported from hermes-peer's hermes_peer/artifacts.py, V14)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_hub.artifacts import (
    INLINE_MAX_BYTES,
    ArtifactRef,
    fetch_artifact_file,
    load_artifact_metadata,
    store_artifact_bytes,
    store_artifact_file,
)


def test_store_artifact_bytes_computes_sha256_and_size(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    data = b"hello world"
    ref = store_artifact_bytes(task_id="t1", name="out.txt", data=data)
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.size_bytes == len(data)
    assert ref.name == "out.txt"
    assert Path(ref.path).read_bytes() == data


def test_store_artifact_file_matches_store_artifact_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    src = tmp_path / "src.bin"
    payload = bytes(range(256)) * 4
    src.write_bytes(payload)
    ref = store_artifact_file(task_id="t1", path=src)
    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.size_bytes == len(payload)


def test_fetch_artifact_file_verifies_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    data = b"round trip me"
    ref = store_artifact_bytes(task_id="t1", name="a.txt", data=data)
    dest = tmp_path / "fetched" / "a.txt"
    fetch_artifact_file(ref, dest)
    assert dest.read_bytes() == data


def test_fetch_artifact_file_raises_on_hash_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    ref = store_artifact_bytes(task_id="t1", name="a.txt", data=b"original")
    # Corrupt the stored bytes after the fact.
    Path(ref.path).write_bytes(b"corrupted!")
    with pytest.raises(ValueError):
        fetch_artifact_file(ref, tmp_path / "out.txt")


def test_load_artifact_metadata_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    ref = store_artifact_bytes(task_id="t1", name="a.txt", data=b"data")
    loaded = load_artifact_metadata("t1", ref.artifact_id)
    assert loaded is not None
    assert loaded.sha256 == ref.sha256
    assert loaded.artifact_id == ref.artifact_id


def test_load_artifact_metadata_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    assert load_artifact_metadata("nonexistent", "art_x") is None


def test_store_artifact_bytes_preserves_caller_supplied_artifact_id(tmp_path, monkeypatch):
    """Regression: the router relays a specific artifact_id (from the
    wire protocol) into storage. If storage silently mints its own random
    id instead, the download URL the router built points at nothing."""
    monkeypatch.setattr("hermes_hub.artifacts._artifact_root", lambda: tmp_path)
    ref = store_artifact_bytes(
        task_id="t1", name="blob.bin", data=b"data", artifact_id="art_caller_supplied"
    )
    assert ref.artifact_id == "art_caller_supplied"
    loaded = load_artifact_metadata("t1", "art_caller_supplied")
    assert loaded is not None
    assert loaded.artifact_id == "art_caller_supplied"


def test_inline_max_bytes_matches_hermes_peer_threshold():
    assert INLINE_MAX_BYTES == 65536
