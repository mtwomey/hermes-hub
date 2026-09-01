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


def test_is_terminal_frame():
    assert is_terminal_frame({"type": "task_complete"}) is True
    assert is_terminal_frame({"type": "task_failed"}) is True
    assert is_terminal_frame({"type": "task_status"}) is False
    assert is_terminal_frame({"type": "register"}) is False
