"""Unit tests for the hermes-hub / hermes-hub-spoke CLIs (M6).

Focused on the pure-logic pieces (argument parsing, JSON-RPC body
construction, SSE parsing) that don't require a live process; the live
end-to-end verification is a separate Gate 6-implementation live test.
"""

from __future__ import annotations

import json

import pytest

from hermes_hub.cli import (
    build_hub_parser,
    build_spoke_parser,
    build_streaming_message_body,
    extract_final_text,
    parse_skill_args,
    parse_sse_event_line,
    spoke_hub_ws_url,
)


def test_hub_parser_serve_defaults():
    parser = build_hub_parser()
    args = parser.parse_args(["serve"])
    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 8770
    assert args.token == ""
    assert args.spoke_token == ""


def test_hub_parser_serve_overrides():
    parser = build_hub_parser()
    args = parser.parse_args(
        [
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--token",
            "abc",
            "--spoke-token",
            "xyz",
        ]
    )
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.token == "abc"
    assert args.spoke_token == "xyz"


def test_hub_parser_ask_matches_h6_shape():
    """H6: 'hermes-hub ask <spoke> "..."' -- callers address a spoke by name."""
    parser = build_hub_parser()
    args = parser.parse_args(["ask", "Olive", "What is on my calendar?"])
    assert args.command == "ask"
    assert args.spoke == "Olive"
    assert args.text == "What is on my calendar?"
    assert args.hub_url == "http://127.0.0.1:8770"
    assert args.context_id == ""


def test_hub_parser_ask_with_context_id_and_hub_url():
    parser = build_hub_parser()
    args = parser.parse_args(
        [
            "ask",
            "Pumpkin",
            "hi",
            "--hub-url",
            "http://192.0.2.236:8770",
            "--context-id",
            "ctx-1",
        ]
    )
    assert args.hub_url == "http://192.0.2.236:8770"
    assert args.context_id == "ctx-1"


def test_build_streaming_message_body_addresses_spoke_via_metadata():
    body = build_streaming_message_body(spoke="Olive", text="What is 9+16?")
    assert body["method"] == "SendStreamingMessage"
    message = body["params"]["message"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "What is 9+16?"}]
    assert message["metadata"]["targetSpoke"] == "Olive"
    assert "contextId" not in message


def test_build_streaming_message_body_includes_context_id_when_given():
    body = build_streaming_message_body(spoke="Olive", text="hi", context_id="ctx-99")
    assert body["params"]["message"]["contextId"] == "ctx-99"


def test_build_streaming_message_body_includes_credential_when_given():
    body = build_streaming_message_body(spoke="Olive", text="hi", credential="opaque-secret")
    assert body["params"]["message"]["metadata"]["spokeCredential"] == "opaque-secret"


def test_build_streaming_message_body_omits_credential_key_when_absent():
    body = build_streaming_message_body(spoke="Olive", text="hi")
    assert "spokeCredential" not in body["params"]["message"]["metadata"]


def test_parse_sse_event_line_ignores_non_data_lines():
    assert parse_sse_event_line("") is None
    assert parse_sse_event_line(": ping - now") is None


def test_parse_sse_event_line_parses_data_payload():
    payload = parse_sse_event_line('data: {"result": {"foo": "bar"}}')
    assert payload == {"result": {"foo": "bar"}}


def test_extract_final_text_returns_none_for_non_completed_status():
    payload = {
        "result": {
            "statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}
        }
    }
    assert extract_final_text(payload) is None


def test_extract_final_text_returns_text_for_completed_status():
    payload = {
        "result": {
            "statusUpdate": {
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"parts": [{"text": "25"}]},
                }
            }
        }
    }
    assert extract_final_text(payload) == "25"


def test_spoke_parser_connect_requires_hub_and_name():
    parser = build_spoke_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["connect"])  # missing required args


def test_spoke_parser_connect_parses_skills():
    parser = build_spoke_parser()
    args = parser.parse_args(
        [
            "connect",
            "--hub",
            "http://192.0.2.236:8770",
            "--name",
            "Olive",
            "--token",
            "secret",
            "--skill",
            "general-reasoning:General reasoning",
            "--skill",
            "filesystem-search",
        ]
    )
    assert args.hub == "http://192.0.2.236:8770"
    assert args.name == "Olive"
    assert args.token == "secret"
    skills = parse_skill_args(args.skills)
    assert skills == [
        {"id": "general-reasoning", "name": "General reasoning"},
        {"id": "filesystem-search", "name": "filesystem-search"},
    ]


def test_spoke_hub_ws_url_converts_http_to_ws_and_appends_path():
    assert (
        spoke_hub_ws_url("http://192.0.2.236:8770")
        == "ws://192.0.2.236:8770/hub/v1/spoke"
    )


def test_spoke_hub_ws_url_converts_https_to_wss():
    assert (
        spoke_hub_ws_url("https://hub.example.com")
        == "wss://hub.example.com/hub/v1/spoke"
    )


def test_spoke_hub_ws_url_idempotent_if_already_ws_with_path():
    assert (
        spoke_hub_ws_url("ws://127.0.0.1:8770/hub/v1/spoke")
        == "ws://127.0.0.1:8770/hub/v1/spoke"
    )
