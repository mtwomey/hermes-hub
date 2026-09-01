"""Gate 3 live test: runs the real hub app (build_hub_app) via uvicorn."""

from __future__ import annotations

import sys

sys.path.insert(0, "/Users/mtwomey/Git_Repos/hermes-hub")

import uvicorn

from hermes_hub.hub_server import build_hub_app

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    app = build_hub_app(base_url=f"http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
