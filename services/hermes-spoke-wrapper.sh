#!/bin/bash
# ai.hermes.spoke launchd wrapper.
#
# A real spoke executes real Hermes agent turns, so it MUST run under the live
# Hermes runtime (not this repo's own .venv, which cannot import run_agent --
# see docs/VISION.md / the V10 plan).
#
# Do NOT pin this service to a venv path. Hermes manages its own runtime: a
# downloaded "store" interpreter under ~/.hermes/tools/ plus content-hashed
# dependency generations under ~/.hermes/installs/. Those paths change on
# update, and the historical single venv (~/.hermes/hermes-agent/venv) is gone
# on current installs. We therefore resolve the interpreter from Hermes itself
# at startup and let hermes_bootstrap activate whichever generation is
# currently selected. Nothing is ever pip installed by this service.
#
# This wrapper fails loudly, before starting anything, if the runtime or the
# transport deps are unusable -- a launchd KeepAlive service that starts and
# then immediately ImportErrors is a crash loop, not a clear failure.
#
# All configuration here is non-secret (paths, port, spoke name). The per-task
# credential (V5a) is resolved at runtime from the macOS Keychain by
# hermes_hub.credentials.resolve_spoke_credential -- it is never passed as an
# argument or environment variable here.
set -euo pipefail

: "${HERMES_HUB_REPO:?HERMES_HUB_REPO must be set}"
HERMES_HUB_PORT="${HERMES_HUB_PORT:-8770}"
HERMES_HUB_SPOKE_NAME="${HERMES_HUB_SPOKE_NAME:-Pumpkin}"
HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}"
export HERMES_AGENT_ROOT

# Resolve the interpreter Hermes itself runs on. Order of preference:
#   1. HERMES_SPOKE_PYTHON  -- explicit override, for unusual installs.
#   2. HERMES_AGENT_VENV    -- legacy single-venv installs, if still present.
#   3. Hermes's own launcher shim, which hardcodes the current store
#      interpreter and is rewritten by Hermes on update.
SPOKE_PYTHON=""
if [ -n "${HERMES_SPOKE_PYTHON:-}" ] && [ -x "${HERMES_SPOKE_PYTHON}" ]; then
    SPOKE_PYTHON="$HERMES_SPOKE_PYTHON"
elif [ -n "${HERMES_AGENT_VENV:-}" ] && [ -x "${HERMES_AGENT_VENV}/bin/python" ]; then
    SPOKE_PYTHON="${HERMES_AGENT_VENV}/bin/python"
else
    # The shim is `exec <store python> -I -c '<bootstrap>'`; take its interpreter.
    HERMES_SHIM="$HERMES_AGENT_ROOT/.hermes/bin/hermes"
    if [ -x "$HERMES_SHIM" ]; then
        SPOKE_PYTHON="$(awk '/^exec /{print $2; exit}' "$HERMES_SHIM" 2>/dev/null || true)"
    fi
fi

if [ -z "$SPOKE_PYTHON" ] || [ ! -x "$SPOKE_PYTHON" ]; then
    echo "hermes-spoke-wrapper: could not resolve a usable Hermes Python interpreter." >&2
    echo "Looked at: \$HERMES_SPOKE_PYTHON, \$HERMES_AGENT_VENV/bin/python, and" >&2
    echo "$HERMES_AGENT_ROOT/.hermes/bin/hermes" >&2
    echo "Check that Hermes is installed and 'hermes --version' works, then retry." >&2
    exit 1
fi

# Check the deps the way the spoke actually gets them: after Hermes has
# activated its managed dependency generation. Testing bare importability on
# $SPOKE_PYTHON is wrong twice over -- hermes_bootstrap re-execs onto Hermes's
# own store interpreter with -I (dropping any starting venv's site-packages),
# and the deps do not exist on that interpreter until activation runs. A bare
# check both passes when the real path would fail and fails when it would work.
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
echo "ai.hermes.spoke startup: name=$HERMES_HUB_SPOKE_NAME hub_port=$HERMES_HUB_PORT python=$SPOKE_PYTHON"
exec "$SPOKE_PYTHON" scripts/real_spoke.py "$HERMES_HUB_PORT" "$HERMES_HUB_SPOKE_NAME"
