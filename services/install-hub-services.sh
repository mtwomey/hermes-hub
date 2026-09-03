#!/bin/bash
# install-hub-services.sh -- install / uninstall / status for the V10
# managed services: ai.hermes.hub and ai.hermes.spoke.
#
# Follows the ~/Git_Repos/hermes-services convention (label namespace,
# wrapper-script indirection, ~/.hermes/logs/<label>.log, explicit
# EnvironmentVariables, KeepAlive/RunAtLoad/ThrottleInterval) without
# writing into that repo -- these are hermes-hub's own deployment
# artifacts (V8: the hub is portable, so its service definition travels
# with it).
#
# NEVER touches ai.hermes.gateway. Does not install anything into
# ~/.hermes/hermes-agent/'s venv -- see hermes-spoke-wrapper.sh for the
# loud-failure check instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_HUB_CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-hub/service.env"
HUB_CONFIG_FILE="${HUB_CONFIG_FILE:-$DEFAULT_HUB_CONFIG_FILE}"

# This is a non-secret, local deployment configuration. Parse only explicit
# assignments so an installer invocation never evaluates arbitrary shell code.
if [ -f "$HUB_CONFIG_FILE" ]; then
    while IFS='=' read -r key value || [ -n "$key" ]; do
        case "$key" in
            ""|\#*) continue ;;
            HUB_HOST|HUB_PORT|HUB_PUBLIC_URL|HUB_TASK_TIMEOUT_SECONDS|SPOKE_NAME)
                printf -v "CONFIG_${key}" '%s' "$value"
                ;;
            *)
                echo "Unsupported setting in $HUB_CONFIG_FILE: $key" >&2
                exit 2
                ;;
        esac
    done < "$HUB_CONFIG_FILE"
fi

HOMES_DIR="${HOMES_DIR:-$HOME/.hermes}"
LOG_DIR="${LOG_DIR:-$HOMES_DIR/logs}"
HUB_VENV="${HUB_VENV:-$REPO_DIR/.venv}"
HERMES_AGENT_VENV="${HERMES_AGENT_VENV:-$HOMES_DIR/hermes-agent/venv}"
HUB_HOST="${HUB_HOST:-${CONFIG_HUB_HOST:-127.0.0.1}}"
HUB_PORT="${HUB_PORT:-${CONFIG_HUB_PORT:-8770}}"
# A wildcard bind address is not a routable endpoint. Operators exposing the
# hub beyond loopback must explicitly provide its stable, reachable base URL.
HUB_PUBLIC_URL="${HUB_PUBLIC_URL:-${CONFIG_HUB_PUBLIC_URL:-}}"
if [ -z "$HUB_PUBLIC_URL" ]; then
    case "$HUB_HOST" in
        0.0.0.0|::)
            echo "HUB_PUBLIC_URL is required when HUB_HOST binds all interfaces" >&2
            exit 2
            ;;
        *) HUB_PUBLIC_URL="http://${HUB_HOST}:${HUB_PORT}" ;;
    esac
fi
HUB_TASK_TIMEOUT_SECONDS="${HUB_TASK_TIMEOUT_SECONDS:-${CONFIG_HUB_TASK_TIMEOUT_SECONDS:-300}}"
SPOKE_NAME="${SPOKE_NAME:-${CONFIG_SPOKE_NAME:-Pumpkin}}"
LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"

HUB_LABEL="ai.hermes.hub"
SPOKE_LABEL="ai.hermes.spoke"

HUB_TEMPLATE="$SCRIPT_DIR/ai.hermes.hub.plist.template"
SPOKE_TEMPLATE="$SCRIPT_DIR/ai.hermes.spoke.plist.template"
HUB_WRAPPER="$SCRIPT_DIR/hermes-hub-wrapper.sh"
SPOKE_WRAPPER="$SCRIPT_DIR/hermes-spoke-wrapper.sh"

HUB_PLIST="$LAUNCH_AGENTS_DIR/${HUB_LABEL}.plist"
SPOKE_PLIST="$LAUNCH_AGENTS_DIR/${SPOKE_LABEL}.plist"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

check_dependencies() {
    log_info "Checking dependencies..."

    if [ ! -d "$HUB_VENV" ]; then
        log_error "Hub venv not found at $HUB_VENV (run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]')"
        exit 1
    fi

    if [ ! -x "$HUB_WRAPPER" ] || [ ! -x "$SPOKE_WRAPPER" ]; then
        log_error "Wrapper scripts must be executable: $HUB_WRAPPER, $SPOKE_WRAPPER"
        exit 1
    fi

    if [ ! -d "$HERMES_AGENT_VENV" ]; then
        log_warn "Hermes runtime venv not found at $HERMES_AGENT_VENV -- the spoke service will fail loudly at start rather than install anything into it."
    fi

    mkdir -p "$LOG_DIR"
    log_info "Dependencies checked"
}

render_template() {
    local template="$1" out="$2"
    sed \
        -e "s#__REPO_DIR__#$REPO_DIR#g" \
        -e "s#__HUB_VENV__#$HUB_VENV#g" \
        -e "s#__HERMES_VENV__#$HERMES_AGENT_VENV#g" \
        -e "s#__HUB_HOST__#$HUB_HOST#g" \
        -e "s#__HUB_PORT__#$HUB_PORT#g" \
        -e "s#__HUB_PUBLIC_URL__#$HUB_PUBLIC_URL#g" \
        -e "s#__HUB_TASK_TIMEOUT_SECONDS__#$HUB_TASK_TIMEOUT_SECONDS#g" \
        -e "s#__SPOKE_NAME__#$SPOKE_NAME#g" \
        -e "s#__LOG_DIR__#$LOG_DIR#g" \
        "$template" > "$out"
}

create_plists() {
    log_info "Generating plists from templates..."
    mkdir -p "$LAUNCH_AGENTS_DIR"

    local tmp_hub tmp_spoke
    tmp_hub="$(mktemp)"
    tmp_spoke="$(mktemp)"

    sed -e "s#__LABEL__#$HUB_LABEL#g" -e "s#__WRAPPER__#$HUB_WRAPPER#g" "$HUB_TEMPLATE" > "$tmp_hub"
    render_template "$tmp_hub" "$HUB_PLIST"
    rm -f "$tmp_hub"

    sed -e "s#__LABEL__#$SPOKE_LABEL#g" -e "s#__WRAPPER__#$SPOKE_WRAPPER#g" "$SPOKE_TEMPLATE" > "$tmp_spoke"
    render_template "$tmp_spoke" "$SPOKE_PLIST"
    rm -f "$tmp_spoke"

    log_info "Plists written: $HUB_PLIST, $SPOKE_PLIST"
}

install_services() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        log_info "DRY RUN: plists generated; launchctl was not called"
        return
    fi

    log_info "Loading services into launchd..."
    USER_ID=$(id -u)

    launchctl enable "gui/$USER_ID/$HUB_LABEL" 2>/dev/null || true
    if ! launchctl bootstrap "gui/$USER_ID" "$HUB_PLIST"; then
        log_error "launchctl failed to load $HUB_LABEL"
        exit 1
    fi
    log_info "$HUB_LABEL loaded"

    launchctl enable "gui/$USER_ID/$SPOKE_LABEL" 2>/dev/null || true
    if ! launchctl bootstrap "gui/$USER_ID" "$SPOKE_PLIST"; then
        log_error "launchctl failed to load $SPOKE_LABEL"
        # Do not leave a partial managed deployment when spoke bootstrap
        # fails. This only targets the new hub label, never the gateway.
        launchctl bootout "gui/$USER_ID/$HUB_LABEL" 2>/dev/null || true
        exit 1
    fi
    log_info "$SPOKE_LABEL loaded"
}

uninstall_services() {
    log_info "Removing services from launchd..."
    USER_ID=$(id -u)

    if [ "${DRY_RUN:-0}" != "1" ]; then
        [ -f "$HUB_PLIST" ] && launchctl bootout "gui/$USER_ID" "$HUB_PLIST" 2>/dev/null || true
        [ -f "$SPOKE_PLIST" ] && launchctl bootout "gui/$USER_ID" "$SPOKE_PLIST" 2>/dev/null || true
        launchctl bootout "gui/$USER_ID/$HUB_LABEL" 2>/dev/null || true
        launchctl bootout "gui/$USER_ID/$SPOKE_LABEL" 2>/dev/null || true
    else
        log_info "DRY RUN: launchctl was not called"
    fi

    rm -f "$HUB_PLIST" "$SPOKE_PLIST"
    log_info "Services uninstalled"
}

show_status() {
    echo ""
    echo "=== V10 hub/spoke service status ==="
    echo "Configuration: $HUB_CONFIG_FILE"
    echo "Requested hub endpoint: $HUB_HOST:$HUB_PORT ($HUB_PUBLIC_URL)"
    echo ""
    for label in "$HUB_LABEL" "$SPOKE_LABEL"; do
        if launchctl list | grep -q "$label"; then
            echo -e "${GREEN}OK${NC} $label is registered"
            launchctl list "$label" | grep -E "PID|LastExitStatus" || true
        else
            echo -e "${RED}--${NC} $label is NOT registered"
        fi
    done
    echo ""
    if lsof -nP -iTCP:"$HUB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo -e "${GREEN}OK${NC} hub port $HUB_PORT is listening"
    else
        echo -e "${RED}--${NC} hub port $HUB_PORT is NOT listening"
    fi
    echo ""
}

show_help() {
    echo "Usage: $0 {install|uninstall|status|reinstall}"
    echo ""
    echo "  install    - Generate plists and load ai.hermes.hub + ai.hermes.spoke"
    echo "  uninstall  - Unload both services and remove their plists"
    echo "  status     - Show registration, PID, and hub port status"
    echo "  reinstall  - uninstall then install"
    echo ""
    echo "Loads non-secret deployment values from $HUB_CONFIG_FILE when present."
    echo "Copy services/hub-service.env.example to that path and set HUB_PUBLIC_URL"
    echo "before using HUB_HOST=0.0.0.0. Environment variables override that file."
    echo ""
    echo "Never touches ai.hermes.gateway. Env overrides: HOMES_DIR, LOG_DIR,"
    echo "HUB_VENV, HERMES_AGENT_VENV, HUB_HOST, HUB_PORT, HUB_PUBLIC_URL,"
    echo "HUB_TASK_TIMEOUT_SECONDS, SPOKE_NAME, LAUNCH_AGENTS_DIR."
}

case "${1:-status}" in
    install)
        check_dependencies
        create_plists
        install_services
        ;;
    uninstall)
        uninstall_services
        ;;
    status)
        show_status
        ;;
    reinstall)
        uninstall_services
        sleep 1
        check_dependencies
        create_plists
        install_services
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "Unknown command: ${1:-}"
        show_help
        exit 1
        ;;
esac
