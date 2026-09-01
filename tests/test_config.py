"""Tests for hermes_hub.config: spoke identity as a config value (Task 1.4).

The local spoke stays named "Pumpkin" -- this host IS Pumpkin. This module
only makes the name configurable (env override) with Pumpkin as the
default; no test here asserts a rename, per V10/plan Task 1.4.
"""

from __future__ import annotations

from hermes_hub.config import (
    ENV_SPOKE_NAME,
    DEFAULT_SPOKE_NAME,
    resolve_spoke_name,
)


def test_default_spoke_name_is_pumpkin():
    assert DEFAULT_SPOKE_NAME == "Pumpkin"


def test_resolve_spoke_name_defaults_to_pumpkin_with_nothing_configured(monkeypatch):
    monkeypatch.delenv(ENV_SPOKE_NAME, raising=False)
    assert resolve_spoke_name() == "Pumpkin"


def test_resolve_spoke_name_reads_env_override(monkeypatch):
    monkeypatch.setenv(ENV_SPOKE_NAME, "SomeOtherSpoke")
    assert resolve_spoke_name() == "SomeOtherSpoke"


def test_explicit_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv(ENV_SPOKE_NAME, "SomeOtherSpoke")
    assert resolve_spoke_name(explicit="Explicit") == "Explicit"


def test_empty_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(ENV_SPOKE_NAME, "")
    assert resolve_spoke_name() == "Pumpkin"
