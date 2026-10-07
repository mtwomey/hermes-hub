"""Behaviour of services/hermes-post-update.sh (docs/POST-UPDATE.md).

Everything runs against fakes: a temp HERMES_HOME with installs/<id>/facts.json,
a fake `launchctl` (list / kickstart) whose state lives in files, a fake `lsof`
that reports which dependency generation a pid has open, a fake `hermes`, and a
real SQLite spoke ledger. Nothing touches the real launchd domain or Hermes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "services" / "hermes-post-update.sh"
OLD, NEW = "a" * 32, "b" * 32

FAKE_LAUNCHCTL = r"""#!/bin/bash
S="$FAKE_STATE"
case "$1" in
  list)
    printf 'PID\tStatus\tLabel\n'
    [ -s "$S/pid" ] && printf '%s\t0\tai.hermes.spoke\n' "$(cat "$S/pid")"
    exit 0 ;;
  kickstart)
    echo "kickstart $*" >> "$S/calls"
    old="$(cat "$S/pid" 2>/dev/null || echo 1000)"
    new=$((old + 1))
    echo "$new" > "$S/pid"
    # the restarted spoke loads whatever generation FAKE_RESTART_ENV says
    echo "$new $(cat "$S/restart_env")" >> "$S/envmap"
    echo "spoke Test: connected and registered" >> "$S/spoke.log"
    exit 0 ;;
esac
exit 0
"""

FAKE_LSOF = r"""#!/bin/bash
# lsof -p PID  -> one line per generation the pid has open
pid="$2"
grep "^$pid " "$FAKE_STATE/envmap" 2>/dev/null | while read -r _ env; do
  echo "python $pid user txt REG 1,2 3 4 /h/.hermes/installs/x/environments/$env/venv/lib/python3.14/site.py"
done
exit 0
"""

FAKE_HERMES = r"""#!/bin/bash
echo "$*" >> "$FAKE_STATE/hermes_calls"
echo "Hermes Agent vTEST"
"""


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "hermes_home"
    inst = home / "installs" / "0123456789abcdef"
    inst.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in (("launchctl", FAKE_LAUNCHCTL), ("lsof", FAKE_LSOF), ("hermes", FAKE_HERMES)):
        (bins / name).write_text(body)
        (bins / name).chmod(0o755)
    ledger = tmp_path / "spoke_ledger.db"
    con = sqlite3.connect(ledger)
    con.execute("CREATE TABLE requests (task_id TEXT, context_id TEXT, caller TEXT, request TEXT, "
                "state TEXT NOT NULL, answer TEXT, started_at REAL, updated_at REAL, answer_len INTEGER)")
    con.commit()
    con.close()

    class Rig:
        def select(self, env_hash: str, age: float = 3600) -> None:
            facts = inst / "facts.json"
            facts.write_text(json.dumps({"packages": {"venv": {
                "environment": f"{inst}/environments/{env_hash}/venv"}}, "schema": 1}))
            t = time.time() - age
            os.utime(facts, (t, t))

        def spoke(self, pid: int, env_hash: str, restart_env: str | None = None) -> None:
            (state / "pid").write_text(f"{pid}\n")
            (state / "envmap").write_text(f"{pid} {env_hash}\n")
            (state / "restart_env").write_text((restart_env or NEW) + "\n")

        def task(self, state_name: str, started_ago: float) -> None:
            con = sqlite3.connect(ledger)
            now = time.time()
            con.execute("INSERT INTO requests VALUES ('t1','c1','x','r',?, '', ?, ?, 0)",
                        (state_name, now - started_ago, now - started_ago))
            con.commit()
            con.close()

        def calls(self, name: str = "calls") -> str:
            f = state / name
            return f.read_text() if f.exists() else ""

        def run(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
            env = dict(os.environ)
            env.update({
                "HERMES_HOME": str(home),
                "HERMES_BIN": str(bins / "hermes"),
                "HERMES_POST_UPDATE_PYTHON": sys.executable,
                "HERMES_POST_UPDATE_SPOKE_LOG": str(state / "spoke.log"),
                "HERMES_POST_UPDATE_LOCK": str(tmp_path / "lock"),
                "HERMES_POST_UPDATE_VERIFY_SECS": "5",
                "HERMES_POST_UPDATE_NOTIFY": "0",
                "HERMES_HUB_SPOKE_LEDGER": str(ledger),
                "LAUNCHCTL": str(bins / "launchctl"),
                "LSOF": str(bins / "lsof"),
                "FAKE_STATE": str(state),
            })
            return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True,
                                  text=True, timeout=timeout, check=False)

    r = Rig()
    r.tmp_path = tmp_path
    return r


def test_help_lists_every_flag(rig):
    out = rig.run("--help").stdout
    for flag in ("--update", "--watch", "--force", "--no-wait", "--wait", "--settle"):
        assert flag in out


def test_spoke_already_current_is_a_no_op(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    res = rig.run()
    assert res.returncode == 0, res.stderr
    assert "no restart needed" in res.stdout
    assert rig.calls() == ""


def test_stale_spoke_is_restarted_and_verified(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    res = rig.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "kickstart -k gui/" in rig.calls() and "/ai.hermes.spoke" in rig.calls()
    assert f"spoke pid 1001 is on {NEW}" in res.stdout
    assert "re-registered with the hub" in res.stdout


def test_manual_mode_finalizes_hermes_first(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    rig.run()
    assert "--version" in rig.calls("hermes_calls")


def test_watch_mode_never_runs_hermes_and_is_silent_when_current(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    res = rig.run("--watch", "--settle", "0")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "" and res.stderr == ""
    assert rig.calls("hermes_calls") == ""


def test_watch_mode_restarts_stale_spoke_without_running_hermes(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    res = rig.run("--watch", "--settle", "0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "kickstart" in rig.calls()
    assert rig.calls("hermes_calls") == ""


def test_watch_mode_waits_for_facts_to_settle(rig):
    rig.select(NEW, age=0)
    rig.spoke(1000, NEW)
    t0 = time.time()
    res = rig.run("--watch", "--settle", "3")
    assert res.returncode == 0, res.stderr
    assert time.time() - t0 >= 2.5


def test_live_in_flight_task_postpones_restart(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    rig.task("working", started_ago=30)
    res = rig.run("--wait", "0")
    assert res.returncode == 2
    assert "still running" in res.stderr
    assert rig.calls() == ""


def test_dead_working_row_older_than_ttl_does_not_block(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    rig.task("working", started_ago=7200)  # spoke died mid-task long ago
    res = rig.run("--wait", "0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "kickstart" in rig.calls()


def test_no_wait_restarts_despite_in_flight_task(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    rig.task("working", started_ago=30)
    res = rig.run("--no-wait")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "kickstart" in rig.calls()


def test_force_restarts_current_spoke(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    res = rig.run("--force")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "restarting anyway (--force)" in res.stdout
    assert "kickstart" in rig.calls()


def test_restart_onto_wrong_generation_fails_loudly(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD, restart_env=OLD)
    res = rig.run()
    assert res.returncode == 1
    assert f"expected {NEW}" in res.stderr


def test_unknown_spoke_environment_is_never_restarted_blind(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    (rig.tmp_path / "state" / "envmap").write_text("")  # spoke has nothing open yet
    res = rig.run("--watch", "--settle", "0", timeout=90)
    assert res.returncode == 0
    assert "cannot tell" in res.stderr
    assert rig.calls() == ""


def test_concurrent_run_exits_without_acting(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    lock = rig.tmp_path / "lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))  # a live holder
    res = rig.run()
    assert res.returncode == 0
    assert "another hermes-post-update" in res.stdout
    assert rig.calls() == ""


def test_stale_lock_from_dead_process_is_reclaimed(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    lock = rig.tmp_path / "lock"
    lock.mkdir()
    (lock / "pid").write_text("999999")  # no such process
    res = rig.run()
    assert res.returncode == 0, res.stderr
    assert "no restart needed" in res.stdout
    assert not lock.exists()


def test_missing_facts_fails_loudly(rig):
    rig.spoke(1000, NEW)
    res = rig.run()
    assert res.returncode == 1
    assert "facts.json" in res.stderr
