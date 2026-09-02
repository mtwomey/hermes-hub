"""Run the managed hub with endpoint configuration and Keychain credentials."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from hermes_hub.hub_runtime import resolve_hub_runtime
from hermes_hub.hub_server import build_hub_app

if __name__ == "__main__":
    runtime = resolve_hub_runtime()
    app = build_hub_app(
        base_url=runtime.base_url,
        expected_external_token=runtime.external_token,
        expected_spoke_token=runtime.spoke_token,
        task_timeout_seconds=runtime.task_timeout_seconds,
    )
    uvicorn.run(app, host=runtime.host, port=runtime.port, log_level="info")
