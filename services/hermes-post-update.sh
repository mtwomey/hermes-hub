#!/usr/bin/env bash
# hermes-post-update — keep this machine's hub spoke (ai.hermes.spoke) on the
# dependency environment Hermes currently has selected.
#
# Why: Hermes keeps its Python deps in content-hashed "generations" under
# ~/.hermes/installs/<id>/environments/<hash>/venv and selects one in
# ~/.hermes/installs/<id>/facts.json. `hermes update` (and some plugin edits)
# build and select a new generation and restart the gateway/desktop, but never
# touch ai.hermes.spoke, which keeps running the old generation until restarted.
#
# Two ways to run it:
#   * By hand, after `hermes update`:   hermes-post-update [--update]
#   * Automatically: the ai.hermes.post-update LaunchAgent runs
#     `hermes-post-update --watch` whenever facts.json's directory changes and
#     every 15 min as a safety net (see docs/POST-UPDATE.md).
#
# Steps:
#   1. (--update) run `hermes update` first.
#   2. (manual mode only) run `hermes --version` so hermes_bootstrap finishes any
#      pending dependency build. --watch never runs hermes, so it can never
#      itself trigger a rebuild.
#   3. (--watch) wait until facts.json has been unchanged for --settle seconds.
#   4. Compare the spoke's loaded generation to the selected one; stop if equal.
#   5. Wait for the spoke's in-flight tasks to finish (spoke ledger rows in
#      state 'working' that are younger than the hub task TTL), unless --no-wait.
#   6. `launchctl kickstart -k` the spoke, verify it loaded the selected
#      generation and re-registered with the hub.
#   7. Report any other Hermes process still on an older generation.
#
# Usage: hermes-post-update [--update] [--watch] [--force] [--no-wait]
#                           [--wait SECONDS] [--settle SECONDS]
#   --watch    unattended mode for launchd: quiet when nothing to do, no hermes
#              invocation, macOS notification when it acts or fails
#   --force    restart the spoke even if it is already on the selected generation
#   --no-wait  restart immediately even if spoke tasks are in flight
#   --wait N   max seconds to wait for in-flight tasks (default 600; 300 in --watch)
#   --settle N seconds facts.json must be unchanged first (default 0; 60 in --watch)
# Exit: 0 ok / nothing to do, 1 failure, 2 in-flight tasks did not finish in time.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HERMES_HOME/hermes-agent}"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
SPOKE_LABEL="${HERMES_POST_UPDATE_SPOKE_LABEL:-ai.hermes.spoke}"
SPOKE_LOG="${HERMES_POST_UPDATE_SPOKE_LOG:-$HERMES_HOME/logs/${SPOKE_LABEL}.error.log}"
LEDGER="${HERMES_HUB_SPOKE_LEDGER:-$HOME/.hermes-hub/spoke_ledger.db}"
TASK_TTL="${HERMES_HUB_TASK_TTL_SECONDS:-1800}"
LOCK_DIR="${HERMES_POST_UPDATE_LOCK:-$HERMES_HOME/locks/hermes-post-update.lock}"
LAUNCHCTL="${LAUNCHCTL:-launchctl}"
LSOF="${LSOF:-lsof}"
VERIFY_SECS="${HERMES_POST_UPDATE_VERIFY_SECS:-90}"

WATCH=0; FORCE=0; NO_WAIT=0; DO_UPDATE=0; WAIT_SECS=""; SETTLE_SECS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --update)  DO_UPDATE=1 ;;
        --watch)   WATCH=1 ;;
        --force)   FORCE=1 ;;
        --no-wait) NO_WAIT=1 ;;
        --wait)    WAIT_SECS="${2:?--wait needs seconds}"; shift ;;
        --settle)  SETTLE_SECS="${2:?--settle needs seconds}"; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
    shift
done
if [ "$WATCH" = 1 ]; then WAIT_SECS="${WAIT_SECS:-300}"; SETTLE_SECS="${SETTLE_SECS:-60}"; fi
WAIT_SECS="${WAIT_SECS:-600}"; SETTLE_SECS="${SETTLE_SECS:-0}"

ts()   { date '+%Y-%m-%d %H:%M:%S'; }
say()  { printf '%s [post-update] %s\n' "$(ts)" "$*"; }
warn() { printf '%s [post-update] WARNING: %s\n' "$(ts)" "$*" >&2; }
notify() {  # macOS banner, unattended mode only; never fatal
    [ "$WATCH" = 1 ] && [ "${HERMES_POST_UPDATE_NOTIFY:-1}" = 1 ] || return 0
    /usr/bin/osascript -e "display notification \"$1\" with title \"Hermes post-update\"" >/dev/null 2>&1 || true
}
die()  { printf '%s [post-update] ERROR: %s\n' "$(ts)" "$*" >&2; notify "Failed: $*"; exit 1; }

# Python for JSON/SQLite: Hermes's own store interpreter, stdlib only.
PY="${HERMES_POST_UPDATE_PYTHON:-$(awk '/^exec /{print $2; exit}' "$HERMES_AGENT_ROOT/.hermes/bin/hermes" 2>/dev/null || true)}"
[ -n "$PY" ] && [ -x "$PY" ] || die "cannot resolve the Hermes interpreter from $HERMES_AGENT_ROOT/.hermes/bin/hermes"

# ---- single instance -------------------------------------------------------
mkdir -p "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
        [ "$WATCH" = 1 ] || say "another hermes-post-update (pid $holder) is running; exiting"
        exit 0
    fi
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR" || die "cannot take lock $LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# ---- helpers ---------------------------------------------------------------
facts_files() { ls "$HERMES_HOME"/installs/*/facts.json 2>/dev/null || true; }

selected_env() {
    "$PY" -I -c '
import glob, json, os, re, sys
for f in sorted(glob.glob(os.path.join(sys.argv[1], "installs", "*", "facts.json"))):
    try:
        env = json.load(open(f))["packages"]["venv"]["environment"]
    except Exception:
        continue
    m = re.search(r"environments/([0-9a-f]+)/venv", env)
    if m:
        print(m.group(1)); break
' "$HERMES_HOME"
}

facts_age() {  # seconds since the newest facts.json changed
    "$PY" -I -c '
import glob, os, sys, time
ms = [os.stat(f).st_mtime for f in glob.glob(os.path.join(sys.argv[1], "installs", "*", "facts.json"))]
print(int(time.time() - max(ms)) if ms else 999999)
' "$HERMES_HOME"
}

envs_of_pid() {  # generation hashes a process has open, space separated
    { "$LSOF" -p "$1" 2>/dev/null || true; } | { grep -oE 'environments/[0-9a-f]+' || true; } \
        | sed 's#environments/##' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

spoke_pid() {
    { "$LAUNCHCTL" list 2>/dev/null || true; } | awk -v l="$SPOKE_LABEL" '$3==l && $1 ~ /^[0-9]+$/ {print $1}'
}

working_tasks() {  # live in-flight spoke tasks (rows older than the hub TTL are dead)
    [ -f "$LEDGER" ] || { echo 0; return; }
    "$PY" -I -c '
import sqlite3, sys, time
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True, timeout=5)
    n = c.execute("SELECT count(*) FROM requests WHERE state IN (\"working\",\"submitted\") AND started_at > ?",
                  (time.time() - float(sys.argv[2]),)).fetchone()[0]
except Exception:
    n = 0
print(n)
' "$LEDGER" "$TASK_TTL"
}

# ---- 1-2. update / finalize (manual mode only) -----------------------------
if [ "$DO_UPDATE" = 1 ]; then
    say "running hermes update..."
    "$HERMES_BIN" update || die "hermes update failed; spoke left untouched"
fi
if [ "$WATCH" = 0 ]; then
    say "finalizing Hermes dependencies (hermes --version)..."
    "$HERMES_BIN" --version >/dev/null 2>&1 || die "hermes --version failed; fix Hermes before restarting the spoke"
fi

[ -n "$(facts_files)" ] || die "no $HERMES_HOME/installs/*/facts.json found; is this a package-managed Hermes install?"

# ---- 3. settle -------------------------------------------------------------
if [ "$SETTLE_SECS" -gt 0 ]; then
    for _ in $(seq 1 60); do
        age="$(facts_age)"
        [ "$age" -ge "$SETTLE_SECS" ] && break
        sleep $(( SETTLE_SECS - age + 1 ))
    done
fi

SEL="$(selected_env)"
[ -n "$SEL" ] || die "could not read the selected environment from $HERMES_HOME/installs/*/facts.json"

# ---- 4. is the spoke current? ----------------------------------------------
PID="$(spoke_pid)"
CUR=""
if [ -n "$PID" ]; then
    for _ in $(seq 1 30); do  # a just-started spoke may not have opened its env yet
        CUR="$(envs_of_pid "$PID")"; [ -n "$CUR" ] && break; sleep 1
    done
    if [ -z "$CUR" ] && [ "$FORCE" = 0 ]; then
        warn "cannot tell which environment spoke pid $PID uses (nothing open under environments/); not restarting"
        exit 0
    fi
    if [ "$CUR" = "$SEL" ] && [ "$FORCE" = 0 ]; then
        [ "$WATCH" = 1 ] || say "spoke (pid $PID) already on $SEL; no restart needed"
        exit 0
    fi
    if [ "$CUR" = "$SEL" ]; then
        say "spoke (pid $PID) already on $SEL; restarting anyway (--force)"
    else
        say "selected environment is $SEL; spoke (pid $PID) is on '${CUR:-unknown}'; restart required"
    fi
else
    say "spoke $SPOKE_LABEL is not running; starting it on $SEL"
fi

# ---- 5. drain --------------------------------------------------------------
if [ "$NO_WAIT" = 0 ]; then
    waited=0
    while [ "$(working_tasks)" -gt 0 ]; do
        if [ "$waited" -ge "$WAIT_SECS" ]; then
            warn "$(working_tasks) spoke task(s) still running after ${WAIT_SECS}s; not restarting (will retry; or run with --no-wait)"
            notify "Spoke restart postponed: a hub task is still running. Will retry."
            exit 2
        fi
        [ "$waited" = 0 ] && say "waiting for $(working_tasks) in-flight spoke task(s) to finish..."
        sleep 10; waited=$((waited + 10))
    done
fi

# ---- 6. restart + verify ---------------------------------------------------
LOG_LINES="$(wc -l < "$SPOKE_LOG" 2>/dev/null | tr -d ' ' || true)"; LOG_LINES="${LOG_LINES:-0}"
say "restarting ${SPOKE_LABEL}..."
"$LAUNCHCTL" kickstart -k "gui/$(id -u)/$SPOKE_LABEL" || die "launchctl kickstart $SPOKE_LABEL failed"
NEW=""
for _ in $(seq 1 30); do NEW="$(spoke_pid)"; [ -n "$NEW" ] && [ "$NEW" != "$PID" ] && break; sleep 1; done
[ -n "$NEW" ] && [ "$NEW" != "$PID" ] || die "spoke did not come back up; check $SPOKE_LOG"

GOT=""
for _ in $(seq 1 "$VERIFY_SECS"); do GOT="$(envs_of_pid "$NEW")"; [ -n "$GOT" ] && break; sleep 1; done
[ "$GOT" = "$SEL" ] || die "spoke pid $NEW loaded '${GOT:-nothing}', expected $SEL"
say "spoke pid $NEW is on $SEL"

REG=0
for _ in $(seq 1 "$VERIFY_SECS"); do
    if tail -n +"$((LOG_LINES + 1))" "$SPOKE_LOG" 2>/dev/null | grep -q 'connected and registered'; then REG=1; break; fi
    sleep 1
done
if [ "$REG" = 1 ]; then say "spoke re-registered with the hub"
else warn "no new 'connected and registered' line in $SPOKE_LOG yet"; fi

# ---- 7. anything else still on an old generation? ---------------------------
STALE=""
for p in $(pgrep -f "$HERMES_HOME/tools/python-" 2>/dev/null || true); do
    e="$(envs_of_pid "$p")"
    if [ -n "$e" ] && [ "$e" != "$SEL" ]; then
        warn "pid $p is still on $e: $(ps -o command= -p "$p" 2>/dev/null | cut -c1-90)"
        STALE=1
    fi
done
if [ -z "$STALE" ]; then
    say "all Hermes processes are on $SEL"
    notify "Spoke moved to the new Hermes environment (${SEL:0:8})."
else
    warn "restart those processes (gateway: hermes gateway restart; desktop app: quit and reopen)"
    notify "Spoke moved to ${SEL:0:8}. Gateway/desktop still on an old environment: restart them."
fi
say "old generations are removed automatically once unused and >24h old (hermes pm gc)"
