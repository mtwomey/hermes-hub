#!/bin/bash
# ai.hermes.spoke launchd wrapper.
#
# A real spoke executes real Hermes agent turns, so it MUST run under the
# live Hermes runtime venv (not this repo's own .venv, which cannot import
# run_agent -- see docs/VISION.md / the V10 plan). Nothing may ever be pip
# installed into that venv; the transport deps (a2a-sdk, websockets,
# uvicorn) are expected to already be present there (installed by
# hermes-peer). This wrapper fails loudly, before starting anything, if
# they are missing -- a launchd KeepAlive service that starts and then
# immediately ImportErrors is a crash loop, not a clear failure.
#
# All configuration here is non-secret (paths, port, spoke name). The
# per-task credential (V5a) is resolved at runtime from the macOS Keychain
# by hermes_hub.credentials.resolve_spoke_credential -- it is never passed
# as an argument or environment variable here.
set -euo pipefail

: "${HERMES_AGENT_VENV:?HERMES_AGENT_VENV must be set}"
: "${HERMES_HUB_REPO:?HERMES_HUB_REPO must be set}"
HERMES_HUB_PORT="${HERMES_HUB_PORT:-8770}"
HERMES_HUB_SPOKE_NAME="${HERMES_HUB_SPOKE_NAME:-Pumpkin}"

SPOKE_PYTHON="$HERMES_AGENT_VENV/bin/python"

if [ ! -x "$SPOKE_PYTHON" ]; then
    echo "hermes-spoke-wrapper: Hermes runtime venv python not found or not executable: $SPOKE_PYTHON" >&2
    exit 1
fi

# Check the deps the way the spoke actually gets them: after Hermes has
# activated its managed dependency generation. Testing bare importability on
# $SPOKE_PYTHON is wrong twice over -- hermes_bootstrap re-execs onto Hermes's
# own store interpreter with -I (dropping this venv's site-packages), and the
# deps do not exist on that interpreter until activation runs. A bare check
# both passes when the real path would fail and fails when it would work.
#
# Only websockets is required here: a2a-sdk is imported by the HUB modules
# (hub_server / hub_executor / agent_card), never by the spoke.
if ! "$SPOKE_PYTHON" -c "
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ.get('HERMES_AGENT_ROOT') or str(Path.home() / '.hermes' / 'hermes-agent'))
import hermes_bootstrap  # activates the selected dependency generation
import websockets
" >/dev/null 2>&1; then
    echo "hermes-spoke-wrapper: websockets is not importable from the live Hermes" >&2
    echo "runtime (after hermes_bootstrap activation). Refusing to start: nothing" >&2
    echo "may be pip installed into the Hermes environment by this service." >&2
    echo "Check that Hermes itself runs ('hermes --version') and that its managed" >&2
    echo "dependency generation is current, then retry." >&2
    exit 1
fi

cd "$HERMES_HUB_REPO"
echo "ai.hermes.spoke startup: name=$HERMES_HUB_SPOKE_NAME hub_port=$HERMES_HUB_PORT hermes_venv=$HERMES_AGENT_VENV"
exec "$SPOKE_PYTHON" scripts/real_spoke.py "$HERMES_HUB_PORT" "$HERMES_HUB_SPOKE_NAME"
