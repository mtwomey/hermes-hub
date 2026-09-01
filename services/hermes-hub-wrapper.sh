#!/bin/bash
# ai.hermes.hub launchd wrapper.
#
# The hub has no Hermes core dependency (V8) and runs from this repo's own
# .venv. All configuration arrives via environment variables set in the
# generated plist's EnvironmentVariables block -- none of them are secret
# (host, port, and paths only). No credential may ever be passed here.
set -euo pipefail

: "${HERMES_HUB_VENV:?HERMES_HUB_VENV must be set}"
: "${HERMES_HUB_REPO:?HERMES_HUB_REPO must be set}"
HERMES_HUB_HOST="${HERMES_HUB_HOST:-127.0.0.1}"
HERMES_HUB_PORT="${HERMES_HUB_PORT:-8770}"

HUB_PYTHON="$HERMES_HUB_VENV/bin/python"

if [ ! -x "$HUB_PYTHON" ]; then
    echo "hermes-hub-wrapper: hub venv python not found or not executable: $HUB_PYTHON" >&2
    exit 1
fi

cd "$HERMES_HUB_REPO"
exec "$HUB_PYTHON" scripts/run_hub.py "$HERMES_HUB_PORT"
