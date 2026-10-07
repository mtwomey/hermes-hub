"""Phase 2 Gate 2 (BEA-305): throwaway hub on a NON-production port.

Tokens come from the environment (GATE_EXT_TOKEN, GATE_JOIN_TOKEN), generated
per run into a chmod-600 file; never the production Keychain secrets.
Usage: GATE_EXT_TOKEN=.. GATE_JOIN_TOKEN=.. python scripts/gate_phase2_hub.py 18771
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from hermes_hub.hub_server import build_hub_app

if __name__ == "__main__":
    port = int(sys.argv[1])
    logging.basicConfig(level=logging.INFO, format="[gate-hub] %(asctime)s %(name)s %(message)s")
    app = build_hub_app(
        base_url=f"http://127.0.0.1:{port}",
        expected_external_token=os.environ["GATE_EXT_TOKEN"],
        expected_spoke_token=os.environ["GATE_JOIN_TOKEN"],
        task_timeout_seconds=600.0,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
