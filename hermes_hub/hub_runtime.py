"""Managed hub runtime configuration for W4 LAN exposure."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .credentials import require_hub_credentials

logger = logging.getLogger(__name__)

#: Hard time-to-live for one routed task (Phase 1 / D5, BEA-304). A task runs
#: until the spoke sends a terminal frame or this TTL elapses -- never fails
#: merely because a caller stopped waiting. At TTL the task ends FAILED with
#: ``hermesError=ttl_expired``. Configure via HERMES_HUB_TASK_TTL_SECONDS.
DEFAULT_TASK_TTL_SECONDS = 1800.0
TTL_ENV = "HERMES_HUB_TASK_TTL_SECONDS"
#: Deprecated alias (pre-Phase-1 name, was a 300 s failure timeout).
DEPRECATED_TIMEOUT_ENV = "HERMES_HUB_TASK_TIMEOUT_SECONDS"
#: Back-compat name for importers of the old constant.
DEFAULT_TASK_TIMEOUT_SECONDS = DEFAULT_TASK_TTL_SECONDS


@dataclass(frozen=True)
class HubRuntime:
    host: str
    port: int
    base_url: str
    external_token: str
    spoke_token: str
    task_ttl_seconds: float

    @property
    def task_timeout_seconds(self) -> float:
        """Deprecated alias for :attr:`task_ttl_seconds`."""
        return self.task_ttl_seconds


def resolve_task_ttl_seconds() -> float:
    value = os.environ.get(TTL_ENV)
    if value:
        return float(value)
    legacy = os.environ.get(DEPRECATED_TIMEOUT_ENV)
    if legacy:
        logger.warning(
            "%s is deprecated; use %s (now a hard task TTL, not a failure timeout)",
            DEPRECATED_TIMEOUT_ENV,
            TTL_ENV,
        )
        return float(legacy)
    return DEFAULT_TASK_TTL_SECONDS


def resolve_hub_runtime() -> HubRuntime:
    """Read non-secret endpoint configuration and fail closed for hub tokens."""
    host = os.environ.get("HERMES_HUB_HOST", "127.0.0.1")
    port = int(os.environ.get("HERMES_HUB_PORT", "8770"))
    base_url = os.environ.get("HERMES_HUB_PUBLIC_URL", f"http://{host}:{port}")
    task_ttl_seconds = resolve_task_ttl_seconds()
    external_token, spoke_token = require_hub_credentials()
    return HubRuntime(
        host, port, base_url.rstrip("/"), external_token, spoke_token, task_ttl_seconds
    )
