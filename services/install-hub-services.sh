#!/bin/bash
# install-hub-services.sh -- install / uninstall / status for the V10
# managed services: ai.hermes.hub, ai.hermes.spoke and (with the spoke)
# ai.hermes.post-update, the watcher that keeps the spoke on Hermes's
# currently selected dependency generation (docs/POST-UPDATE.md).
#
# Follows the ~/Git_Repos/hermes-services convention (label namespace,
# wrapper-script indirection, ~/.hermes/logs/<label>.log, explicit
# EnvironmentVariables, KeepAlive/RunAtLoad/ThrottleInterval) without
# writing into that repo -- these are hermes-hub's own deployment
# artifacts (V8: the hub is portable, so its service definition travels
# with it).
#
# NEVER touches ai.hermes.gateway. Never installs anything into Hermes's
# runtime -- see hermes-spoke-wrapper.sh for the loud-failure check instead.
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
            SERVICE_MODE|HUB_BIND_HOST|SPOKE_HUB_HOST|HUB_HOST|HUB_PORT|HUB_PUBLIC_URL|HUB_TASK_TTL_SECONDS|HUB_TASK_TIMEOUT_SECONDS|SPOKE_NAME)
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
LOCAL_BIN_DIR="${LOCAL_BIN_DIR:-$HOME/.local/bin}"
HUB_BIND_HOST="${HUB_BIND_HOST:-${CONFIG_HUB_BIND_HOST:-${HUB_HOST:-${CONFIG_HUB_HOST:-127.0.0.1}}}}"
SPOKE_HUB_HOST="${SPOKE_HUB_HOST:-${CONFIG_SPOKE_HUB_HOST:-127.0.0.1}}"
HUB_PORT="${HUB_PORT:-${CONFIG_HUB_PORT:-8770}}"
# A wildcard bind address is not a routable endpoint. Operators exposing the
# hub beyond loopback must explicitly provide its stable, reachable base URL.
HUB_PUBLIC_URL="${HUB_PUBLIC_URL:-${CONFIG_HUB_PUBLIC_URL:-}}"
if [ -z "$HUB_PUBLIC_URL" ]; then
    case "$HUB_BIND_HOST" in
        0.0.0.0|::)
            echo "HUB_PUBLIC_URL is required when HUB_HOST binds all interfaces" >&2
            exit 2
            ;;
        *) HUB_PUBLIC_URL="http://${HUB_BIND_HOST}:${HUB_PORT}" ;;
    esac
fi
# Hard task TTL (BEA-304). HUB_TASK_TIMEOUT_SECONDS is a deprecated alias.
LEGACY_TIMEOUT="${HUB_TASK_TIMEOUT_SECONDS:-${CONFIG_HUB_TASK_TIMEOUT_SECONDS:-}}"
if [ -z "${HUB_TASK_TTL_SECONDS:-${CONFIG_HUB_TASK_TTL_SECONDS:-}}" ] && [ -n "$LEGACY_TIMEOUT" ]; then
    echo "WARNING: HUB_TASK_TIMEOUT_SECONDS is deprecated; using it as HUB_TASK_TTL_SECONDS=$LEGACY_TIMEOUT (default 1800)" >&2
fi
HUB_TASK_TTL_SECONDS="${HUB_TASK_TTL_SECONDS:-${CONFIG_HUB_TASK_TTL_SECONDS:-${LEGACY_TIMEOUT:-1800}}}"
SPOKE_NAME="${SPOKE_NAME:-${CONFIG_SPOKE_NAME:-Pumpkin}}"
SERVICE_MODE="${SERVICE_MODE:-${CONFIG_SERVICE_MODE:-both}}"
case "$SERVICE_MODE" in hub|spoke|both) ;; *) echo "SERVICE_MODE must be hub, spoke, or both" >&2; exit 2 ;; esac
LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"

HUB_LABEL="ai.hermes.hub"
SPOKE_LABEL="ai.hermes.spoke"
POST_UPDATE_LABEL="ai.hermes.post-update"

HUB_TEMPLATE="$SCRIPT_DIR/ai.hermes.hub.plist.template"
SPOKE_TEMPLATE="$SCRIPT_DIR/ai.hermes.spoke.plist.template"
HUB_WRAPPER="$SCRIPT_DIR/hermes-hub-wrapper.sh"
SPOKE_WRAPPER="$SCRIPT_DIR/hermes-spoke-wrapper.sh"
POST_UPDATE_TEMPLATE="$SCRIPT_DIR/ai.hermes.post-update.plist.template"
POST_UPDATE_SCRIPT="$SCRIPT_DIR/hermes-post-update.sh"

HUB_PLIST="$LAUNCH_AGENTS_DIR/${HUB_LABEL}.plist"
SPOKE_PLIST="$LAUNCH_AGENTS_DIR/${SPOKE_LABEL}.plist"
POST_UPDATE_PLIST="$LAUNCH_AGENTS_DIR/${POST_UPDATE_LABEL}.plist"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

has_spoke() { [ "$SERVICE_MODE" = spoke ] || [ "$SERVICE_MODE" = both ]; }

check_dependencies() {
    log_info "Checking dependencies..."

    if { [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; } && [ ! -d "$HUB_VENV" ]; then
        log_error "Hub venv not found at $HUB_VENV (run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]')"
        exit 1
    fi

    if { [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; } && [ ! -x "$HUB_WRAPPER" ]; then
        log_error "Hub wrapper must be executable: $HUB_WRAPPER"
        exit 1
    fi
    if { [ "$SERVICE_MODE" = spoke ] || [ "$SERVICE_MODE" = both ]; } && [ ! -x "$SPOKE_WRAPPER" ]; then
        log_error "Spoke wrapper must be executable: $SPOKE_WRAPPER"
        exit 1
    fi

    if has_spoke && [ ! -x "$POST_UPDATE_SCRIPT" ]; then
        log_error "Post-update script must be executable: $POST_UPDATE_SCRIPT"
        exit 1
    fi

    mkdir -p "$LOG_DIR"
    log_info "Dependencies checked"
}

render_template() {
    local template="$1" out="$2"
    sed \
        -e "s#__REPO_DIR__#$REPO_DIR#g" \
        -e "s#__HUB_VENV__#$HUB_VENV#g" \
        -e "s#__HOMES_DIR__#$HOMES_DIR#g" \
        -e "s#__POST_UPDATE_SCRIPT__#$POST_UPDATE_SCRIPT#g" \
        -e "s#__HUB_BIND_HOST__#$HUB_BIND_HOST#g" \
        -e "s#__SPOKE_HUB_HOST__#$SPOKE_HUB_HOST#g" \
        -e "s#__HUB_PORT__#$HUB_PORT#g" \
        -e "s#__HUB_PUBLIC_URL__#$HUB_PUBLIC_URL#g" \
        -e "s#__HUB_TASK_TTL_SECONDS__#$HUB_TASK_TTL_SECONDS#g" \
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

    if [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; then
        sed -e "s#__LABEL__#$HUB_LABEL#g" -e "s#__WRAPPER__#$HUB_WRAPPER#g" "$HUB_TEMPLATE" > "$tmp_hub"
        render_template "$tmp_hub" "$HUB_PLIST"
    fi
    rm -f "$tmp_hub"

    if [ "$SERVICE_MODE" = spoke ] || [ "$SERVICE_MODE" = both ]; then
        sed -e "s#__LABEL__#$SPOKE_LABEL#g" -e "s#__WRAPPER__#$SPOKE_WRAPPER#g" "$SPOKE_TEMPLATE" > "$tmp_spoke"
        render_template "$tmp_spoke" "$SPOKE_PLIST"
    fi
    rm -f "$tmp_spoke"

    if has_spoke; then
        create_post_update_plist
    fi

    log_info "Plists written: $HUB_PLIST, $SPOKE_PLIST"
}

# The watcher fires when the directory holding facts.json changes. Each Hermes
# checkout has its own install-state dir (~/.hermes/installs/<id>/); watch all
# that exist, or the installs/ root on a machine that has none yet (the 15-min
# StartInterval covers anything a watch misses).
post_update_watch_paths() {
    local found=0 d
    for d in "$HOMES_DIR"/installs/*/; do
        [ -f "${d}facts.json" ] || continue
        printf '        <string>%s</string>\n' "${d%/}"
        found=1
    done
    if [ "$found" = 0 ]; then
        printf '        <string>%s</string>\n' "$HOMES_DIR/installs"
    fi
}

create_post_update_plist() {
    mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"
    local tmp paths
    tmp="$(mktemp)"
    paths="$(mktemp)"
    post_update_watch_paths > "$paths"
    sed -e "s#__LABEL__#$POST_UPDATE_LABEL#g" "$POST_UPDATE_TEMPLATE" \
        | awk -v f="$paths" '/^__WATCH_PATHS__$/ { while ((getline l < f) > 0) print l; next } { print }' > "$tmp"
    render_template "$tmp" "$POST_UPDATE_PLIST"
    rm -f "$tmp" "$paths"
    log_info "Plist written: $POST_UPDATE_PLIST"
}

link_post_update_command() {
    [ "${DRY_RUN:-0}" = "1" ] && return 0
    if [ -d "$LOCAL_BIN_DIR" ]; then
        ln -sfn "$POST_UPDATE_SCRIPT" "$LOCAL_BIN_DIR/hermes-post-update"
        log_info "Linked $LOCAL_BIN_DIR/hermes-post-update -> $POST_UPDATE_SCRIPT"
    else
        log_warn "$LOCAL_BIN_DIR does not exist; run $POST_UPDATE_SCRIPT directly for manual use"
    fi
}

unlink_post_update_command() {
    local link="$LOCAL_BIN_DIR/hermes-post-update"
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$POST_UPDATE_SCRIPT" ]; then
        rm -f "$link"
    fi
}

# Idempotent: reloads the watcher if it is already loaded. Never touches the
# hub or spoke services.
load_post_update() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        log_info "DRY RUN: $POST_UPDATE_LABEL plist generated; launchctl was not called"
        return
    fi
    local user_id
    user_id=$(id -u)
    launchctl bootout "gui/$user_id/$POST_UPDATE_LABEL" 2>/dev/null || true
    launchctl enable "gui/$user_id/$POST_UPDATE_LABEL" 2>/dev/null || true
    if ! launchctl bootstrap "gui/$user_id" "$POST_UPDATE_PLIST"; then
        log_error "launchctl failed to load $POST_UPDATE_LABEL"
        exit 1
    fi
    log_info "$POST_UPDATE_LABEL loaded"
}

unload_post_update() {
    if [ "${DRY_RUN:-0}" != "1" ]; then
        local user_id
        user_id=$(id -u)
        [ -f "$POST_UPDATE_PLIST" ] && launchctl bootout "gui/$user_id" "$POST_UPDATE_PLIST" 2>/dev/null || true
        launchctl bootout "gui/$user_id/$POST_UPDATE_LABEL" 2>/dev/null || true
    fi
    rm -f "$POST_UPDATE_PLIST"
    unlink_post_update_command
}

install_services() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        log_info "DRY RUN: plists generated; launchctl was not called"
        return
    fi

    log_info "Loading services into launchd..."
    USER_ID=$(id -u)

    if [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; then
        launchctl enable "gui/$USER_ID/$HUB_LABEL" 2>/dev/null || true
        if ! launchctl bootstrap "gui/$USER_ID" "$HUB_PLIST"; then
            log_error "launchctl failed to load $HUB_LABEL"
            exit 1
        fi
        log_info "$HUB_LABEL loaded"
    fi

    if [ "$SERVICE_MODE" = spoke ] || [ "$SERVICE_MODE" = both ]; then
        launchctl enable "gui/$USER_ID/$SPOKE_LABEL" 2>/dev/null || true
        if ! launchctl bootstrap "gui/$USER_ID" "$SPOKE_PLIST"; then
            log_error "launchctl failed to load $SPOKE_LABEL"
            if [ "$SERVICE_MODE" = both ]; then
                launchctl bootout "gui/$USER_ID/$HUB_LABEL" 2>/dev/null || true
            fi
            exit 1
        fi
        log_info "$SPOKE_LABEL loaded"
        load_post_update
    fi
}

uninstall_services() {
    log_info "Removing services from launchd..."
    USER_ID=$(id -u)

    if [ "${DRY_RUN:-0}" != "1" ]; then
        if [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; then
            [ -f "$HUB_PLIST" ] && launchctl bootout "gui/$USER_ID" "$HUB_PLIST" 2>/dev/null || true
            launchctl bootout "gui/$USER_ID/$HUB_LABEL" 2>/dev/null || true
            rm -f "$HUB_PLIST"
        fi
        if [ "$SERVICE_MODE" = spoke ] || [ "$SERVICE_MODE" = both ]; then
            [ -f "$SPOKE_PLIST" ] && launchctl bootout "gui/$USER_ID" "$SPOKE_PLIST" 2>/dev/null || true
            launchctl bootout "gui/$USER_ID/$SPOKE_LABEL" 2>/dev/null || true
            rm -f "$SPOKE_PLIST"
        fi
    else
        log_info "DRY RUN: launchctl was not called"
    fi
    if has_spoke; then
        unload_post_update
    fi

    log_info "Services uninstalled"
}

show_status() {
    echo ""
    echo "=== V10 hub/spoke service status ==="
    echo "Configuration: $HUB_CONFIG_FILE"
    echo "Service mode: $SERVICE_MODE"
    if [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; then
        echo "Hub endpoint: $HUB_BIND_HOST:$HUB_PORT ($HUB_PUBLIC_URL)"
    fi
    if [ "$SERVICE_MODE" = spoke ] || [ "$SERVICE_MODE" = both ]; then
        echo "Spoke '$SPOKE_NAME' hub target: $SPOKE_HUB_HOST:$HUB_PORT"
    fi
    echo ""
    local user_id
    user_id=$(id -u)
    for label in "$HUB_LABEL" "$SPOKE_LABEL" "$POST_UPDATE_LABEL"; do
        [ "$label" = "$HUB_LABEL" ] && [ "$SERVICE_MODE" = spoke ] && continue
        [ "$label" != "$HUB_LABEL" ] && [ "$SERVICE_MODE" = hub ] && continue
        if launchctl print "gui/$user_id/$label" >/dev/null 2>&1; then
            echo -e "${GREEN}OK${NC} $label is registered"
            launchctl print "gui/$user_id/$label" | grep -E "pid =|last exit code" || true
        else
            echo -e "${RED}--${NC} $label is NOT registered"
        fi
    done
    if has_spoke && [ -f "$LOG_DIR/$POST_UPDATE_LABEL.log" ]; then
        echo ""
        echo "Last $POST_UPDATE_LABEL activity:"
        tail -n 3 "$LOG_DIR/$POST_UPDATE_LABEL.log" | sed 's/^/  /'
    fi
    echo ""
    if [ "$SERVICE_MODE" = hub ] || [ "$SERVICE_MODE" = both ]; then
        if lsof -nP -iTCP:"$HUB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            echo -e "${GREEN}OK${NC} hub port $HUB_PORT is listening"
        else
            echo -e "${RED}--${NC} hub port $HUB_PORT is NOT listening"
        fi
    fi
    echo ""
}

show_help() {
    echo "Usage: $0 {install|uninstall|status|reinstall|install-watcher|uninstall-watcher}"
    echo ""
    echo "  install           - Generate plists and load the services selected by SERVICE_MODE"
    echo "                      (spoke/both also install the $POST_UPDATE_LABEL watcher)"
    echo "  uninstall         - Unload the services selected by SERVICE_MODE and remove their plists"
    echo "  status            - Show registration, PID, and hub port status for the selected mode"
    echo "  reinstall         - uninstall then install"
    echo "  install-watcher   - (Re)install only $POST_UPDATE_LABEL; never touches hub or spoke"
    echo "  uninstall-watcher - Remove only $POST_UPDATE_LABEL"
    echo ""
    echo "SERVICE_MODE selects which labels are managed: hub, spoke, or both (default)."
    echo ""
    echo "Loads non-secret deployment values from $HUB_CONFIG_FILE when present."
    echo "Copy services/hub-service.env.example to that path and set HUB_PUBLIC_URL"
    echo "before using HUB_BIND_HOST=0.0.0.0. Environment variables override that file."
    echo "See docs/DEPLOYMENT.md for hub-only, spoke-only, and combined setup guides."
    echo ""
    echo "Never touches ai.hermes.gateway. Env overrides: HOMES_DIR, LOG_DIR,"
    echo "HUB_VENV, SERVICE_MODE, HUB_BIND_HOST, SPOKE_HUB_HOST, HUB_PORT,"
    echo "HUB_PUBLIC_URL, HUB_TASK_TTL_SECONDS, SPOKE_NAME, LAUNCH_AGENTS_DIR, LOCAL_BIN_DIR."
}

case "${1:-status}" in
    install)
        check_dependencies
        create_plists
        install_services
        if has_spoke; then link_post_update_command; fi
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
        if has_spoke; then link_post_update_command; fi
        ;;
    install-watcher)
        [ -x "$POST_UPDATE_SCRIPT" ] || { log_error "Post-update script must be executable: $POST_UPDATE_SCRIPT"; exit 1; }
        create_post_update_plist
        load_post_update
        link_post_update_command
        ;;
    uninstall-watcher)
        unload_post_update
        log_info "$POST_UPDATE_LABEL removed"
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
