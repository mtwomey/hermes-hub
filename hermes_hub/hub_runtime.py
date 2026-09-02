"""Managed hub runtime configuration for W4 LAN exposure."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .credentials import require_hub_credentials


@dataclass(frozen=True)
class HubRuntime:
    host: str
    port: int
    base_url: str
    external_token: str
    spoke_token: str


def resolve_hub_runtime() -> HubRuntime:
    """Read non-secret endpoint configuration and fail closed for hub tokens."""
    host = os.environ.get("HERMES_HUB_HOST", "127.0.0.1")
    port = int(os.environ.get("HERMES_HUB_PORT", "8770"))
    base_url = os.environ.get("HERMES_HUB_PUBLIC_URL", f"http://{host}:{port}")
    external_token, spoke_token = require_hub_credentials()
    return HubRuntime(host, port, base_url.rstrip("/"), external_token, spoke_token)
