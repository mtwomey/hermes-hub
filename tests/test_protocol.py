from hermes_hub.protocol import (
    build_task_artifact_frame,
    build_task_complete_frame,
    build_task_failed_frame,
    build_task_frame,
    build_task_status_frame,
    is_terminal_frame,
)


def test_build_task_frame_shape():
    frame = build_task_frame(
        task_id="t1", context_id="c1", text="What is 9+16?", metadata={"targetSpoke": "Olive"}
    )
    assert frame == {
        "type": "task",
        "task_id": "t1",
        "context_id": "c1",
        "text": "What is 9+16?",
        "metadata": {"targetSpoke": "Olive"},
        "credential": "",
    }


def test_task_frame_carries_opaque_credential():
    frame = build_task_frame(
        task_id="t1", context_id="c1", text="hi", credential="opaque-blob"
    )
    assert frame["credential"] == "opaque-blob"


def test_task_frame_credential_defaults_empty():
    frame = build_task_frame(task_id="t1", context_id="c1", text="hi")
    assert frame["credential"] == ""


def test_build_task_status_frame_defaults_to_working():
    frame = build_task_status_frame(task_id="t1")
    assert frame == {"type": "task_status", "task_id": "t1", "state": "working"}


def test_build_task_complete_frame():
    frame = build_task_complete_frame(task_id="t1", text="25")
    assert frame == {"type": "task_complete", "task_id": "t1", "text": "25"}


def test_build_task_failed_frame():
    frame = build_task_failed_frame(task_id="t1", error="boom")
    assert frame == {"type": "task_failed", "task_id": "t1", "error": "boom"}


def test_build_task_artifact_frame():
    frame = build_task_artifact_frame(task_id="t1", artifact_id="a1", name="out.txt", text="hi")
    assert frame["type"] == "task_artifact"
    assert frame["artifact_id"] == "a1"
    assert frame["text"] == "hi"


def test_build_task_artifact_frame_binary_capable():
    """Task 2.2: build_task_artifact_frame must also support small binary
    artifacts inline, base64-encoded and hash-verifiable -- not text-only."""
    raw = bytes(range(256))  # not valid UTF-8
    frame = build_task_artifact_frame(
        task_id="t1",
        artifact_id="a1",
        name="small.bin",
        data=raw,
        mime_type="application/octet-stream",
    )
    assert frame["type"] == "task_artifact"
    import base64
    import hashlib

    assert base64.b64decode(frame["data"]) == raw
    assert frame["sha256"] == hashlib.sha256(raw).hexdigest()
    assert frame["mime_type"] == "application/octet-stream"


def test_build_artifact_begin_frame_shape():
    from hermes_hub.protocol import build_artifact_begin_frame

    frame = build_artifact_begin_frame(
        task_id="t1",
        artifact_id="a1",
        name="blob.bin",
        mime_type="application/octet-stream",
        total_bytes=500,
        sha256="deadbeef",
    )
    assert frame == {
        "type": "artifact_begin",
        "task_id": "t1",
        "artifact_id": "a1",
        "name": "blob.bin",
        "mime_type": "application/octet-stream",
        "total_bytes": 500,
        "sha256": "deadbeef",
    }


def test_build_artifact_chunk_frame_base64_encodes_data():
    from hermes_hub.protocol import build_artifact_chunk_frame

    raw = bytes(range(256))  # not valid UTF-8 -- the whole point
    frame = build_artifact_chunk_frame(task_id="t1", artifact_id="a1", seq=0, data=raw)
    assert frame["type"] == "artifact_chunk"
    assert frame["task_id"] == "t1"
    assert frame["artifact_id"] == "a1"
    assert frame["seq"] == 0
    import base64

    assert base64.b64decode(frame["data"]) == raw


def test_build_artifact_end_frame_shape():
    from hermes_hub.protocol import build_artifact_end_frame

    frame = build_artifact_end_frame(task_id="t1", artifact_id="a1")
    assert frame == {"type": "artifact_end", "task_id": "t1", "artifact_id": "a1"}


def test_chunk_and_reassemble_binary_roundtrip_non_utf8():
    """The whole point of Task 2.2: a payload that is NOT valid UTF-8 must
    survive begin/chunk/end -> reassembly byte-identical. A text-only path
    would silently corrupt this."""
    from hermes_hub.protocol import (
        CHUNK_BYTES,
        build_artifact_chunk_frame,
        chunk_artifact_bytes,
        reassemble_artifact_chunks,
    )

    raw = bytes(range(256)) * 2000  # ~512KB, well over one chunk, not UTF-8
    chunks = list(chunk_artifact_bytes(raw, chunk_bytes=CHUNK_BYTES))
    assert len(chunks) > 1  # must actually span multiple chunks
    frames = [
        build_artifact_chunk_frame(task_id="t1", artifact_id="a1", seq=i, data=c)
        for i, c in enumerate(chunks)
    ]
    reassembled = reassemble_artifact_chunks(frames)
    assert reassembled == raw


def test_reassemble_artifact_chunks_orders_by_seq_not_arrival_order():
    from hermes_hub.protocol import build_artifact_chunk_frame, reassemble_artifact_chunks

    part_a = b"AAAA"
    part_b = b"BBBB"
    frames_out_of_order = [
        build_artifact_chunk_frame(task_id="t1", artifact_id="a1", seq=1, data=part_b),
        build_artifact_chunk_frame(task_id="t1", artifact_id="a1", seq=0, data=part_a),
    ]
    assert reassemble_artifact_chunks(frames_out_of_order) == part_a + part_b


def test_chunk_bytes_and_inline_max_bytes_constants():
    from hermes_hub.protocol import CHUNK_BYTES, INLINE_MAX_BYTES

    assert CHUNK_BYTES == 262144
    assert INLINE_MAX_BYTES == 65536


def test_is_terminal_frame():
    assert is_terminal_frame({"type": "task_complete"}) is True
    assert is_terminal_frame({"type": "task_failed"}) is True
    assert is_terminal_frame({"type": "task_status"}) is False
    assert is_terminal_frame({"type": "register"}) is False
