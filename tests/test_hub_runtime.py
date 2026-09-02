"""W4 M1: managed LAN hub runtime configuration must fail closed."""

from __future__ import annotations

import pytest


def test_managed_hub_config_uses_explicit_bind_and_public_urls(monkeypatch):
    from hermes_hub.hub_runtime import resolve_hub_runtime

    monkeypatch.setenv("HERMES_HUB_HOST", "0.0.0.0")
    monkeypatch.setenv("HERMES_HUB_PORT", "8770")
    monkeypatch.setenv("HERMES_HUB_PUBLIC_URL", "http://192.0.2.236:8770")
    monkeypatch.setattr(
        "hermes_hub.hub_runtime.require_hub_credentials",
        lambda: ("external", "spoke"),
    )

    runtime = resolve_hub_runtime()

    assert runtime.host == "0.0.0.0"
    assert runtime.port == 8770
    assert runtime.base_url == "http://192.0.2.236:8770"
    assert runtime.external_token == "external"
    assert runtime.spoke_token == "spoke"


def test_managed_hub_config_fails_closed_when_keychain_credentials_unavailable(monkeypatch):
    from hermes_hub.credentials import CredentialUnavailable
    from hermes_hub.hub_runtime import resolve_hub_runtime

    monkeypatch.delenv("HERMES_HUB_HOST", raising=False)
    monkeypatch.setattr(
        "hermes_hub.hub_runtime.require_hub_credentials",
        lambda: (_ for _ in ()).throw(CredentialUnavailable("missing")),
    )

    with pytest.raises(CredentialUnavailable, match="missing"):
        resolve_hub_runtime()
