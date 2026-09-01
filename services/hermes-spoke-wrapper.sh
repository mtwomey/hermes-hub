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

if ! "$SPOKE_PYTHON" -c "import a2a, websockets, uvicorn" >/dev/null 2>&1; then
    echo "hermes-spoke-wrapper: the Hermes runtime venv at $HERMES_AGENT_VENV is missing" >&2
    echo "a2a-sdk / websockets / uvicorn. Refusing to start: nothing may be pip" >&2
    echo "installed into that venv by this service. Install the transport deps" >&2
    echo "the same way hermes-peer did, then retry." >&2
    exit 1
fi

cd "$HERMES_HUB_REPO"
exec "$SPOKE_PYTHON" scripts/real_spoke.py "$HERMES_HUB_PORT" "$HERMES_HUB_SPOKE_NAME"
