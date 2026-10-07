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
# Simulate Hermes switching facts.json while the watcher is mid-run.
if [ -f "$FAKE_STATE/flip_to" ]; then
  cat "$FAKE_STATE/flip_to" > "$FAKE_FACTS"
  rm -f "$FAKE_STATE/flip_to"
fi
grep "^$pid " "$FAKE_STATE/envmap" 2>/dev/null | while read -r _ env; do
  echo "python $pid user txt REG 1,2 3 4 /h/.hermes/installs/x/environments/$env/venv/lib/python3.14/site.py"
done
exit 0
"""

FAKE_HERMES = r"""#!/bin/bash
echo "$*" >> "$FAKE_STATE/hermes_calls"
echo "Hermes Agent vTEST"
"""

# Stand-ins for the Hermes modules the cleanup step imports (the real collector
# must never run against a developer's real install from a test). Generations
# listed in $FAKE_STATE/leased count as in use; FAKE_GC_FAIL makes it raise.
STUB_MODULES = {
    "hermes_cli/__init__.py": "",
    "pm/__init__.py": "",
    "pm/environments.py": """
import json, os
from pathlib import Path

def install_state_dir(project_root):
    return Path(os.environ["HERMES_HOME"]) / "installs" / "0123456789abcdef"

def selected_venv(project_root):
    facts = json.loads((install_state_dir(project_root) / "facts.json").read_text())
    return Path(facts["packages"]["venv"]["environment"])
""",
    "pm/runtime.py": """
def collect_runtime_generations(root):
    return []
""",
    "pm/filesystem.py": """
import fcntl

def lock_fd(fd, *, wait, timeout=None):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
""",
    "hermes_cli/runtime_state.py": """
import os, shutil
from pathlib import Path
from pm.environments import install_state_dir, selected_venv

STATE = Path(os.environ["FAKE_STATE"])

def leases_held(generation):
    leased = (STATE / "leased").read_text().split() if (STATE / "leased").exists() else []
    return generation.name in leased

def collect_generations(project, *, min_age_seconds=86400):
    with open(STATE / "gc_calls", "a") as f:
        f.write(f"min_age={min_age_seconds}" + chr(10))
    if os.environ.get("FAKE_GC_FAIL"):
        raise OSError("simulated collector failure")
    selected = selected_venv(project).parent.resolve()
    removed = []
    for g in (install_state_dir(project) / "environments").iterdir():
        if g.resolve() != selected and not leases_held(g):
            shutil.rmtree(g)
            removed.append(g)
    return removed
""",
}


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
    agent_root = tmp_path / "hermes-agent"
    for rel, body in STUB_MODULES.items():
        (agent_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent_root / rel).write_text(body)
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

        def generation(self, env_hash: str, *, leased: bool = False, size: int = 1024) -> Path:
            g = inst / "environments" / env_hash
            (g / "venv").mkdir(parents=True, exist_ok=True)
            (g / "venv" / "blob").write_bytes(b"x" * size)
            if leased:
                with open(state / "leased", "a") as f:
                    f.write(env_hash + "\n")
            return g

        def flip_selection_mid_run(self, env_hash: str) -> None:
            (state / "flip_to").write_text(json.dumps({"packages": {"venv": {
                "environment": f"{inst}/environments/{env_hash}/venv"}}, "schema": 1}))

        def install_lock_path(self) -> Path:
            return inst / ".install.lock"

        def calls(self, name: str = "calls") -> str:
            f = state / name
            return f.read_text() if f.exists() else ""

        def run(self, *args: str, timeout: int = 60, env_extra: dict | None = None) -> subprocess.CompletedProcess:
            env = dict(os.environ)
            env.update(env_extra or {})
            env.update({
                "HERMES_HOME": str(home),
                "HERMES_AGENT_ROOT": str(agent_root),
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
                "FAKE_FACTS": str(inst / "facts.json"),
            })
            return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True,
                                  text=True, timeout=timeout, check=False)

    r = Rig()
    r.tmp_path = tmp_path
    r.inst = inst
    return r


def test_help_lists_every_flag(rig):
    out = rig.run("--help").stdout
    for flag in ("--update", "--watch", "--force", "--no-wait", "--no-gc", "--wait", "--settle"):
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


# --- step 8: cleanup of old generations ---------------------------------------


def test_cleanup_removes_unused_old_generation_and_keeps_leased_one(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    selected = rig.generation(NEW)
    unused = rig.generation(OLD, size=3 * 1048576)
    leased = rig.generation("c" * 32, leased=True)
    res = rig.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert not unused.exists()
    assert selected.exists() and leased.exists()
    assert f"removed unused old generation {OLD} (3 MB)" in res.stdout
    assert f"kept old generation {'c' * 32}: still in use" in res.stdout
    # Hermes's 24 h minimum age is not applied; leases are the guard.
    assert rig.calls("gc_calls").strip() == "min_age=0.0"


def test_cleanup_runs_after_a_restart_too(rig):
    rig.select(NEW)
    rig.spoke(1000, OLD)
    rig.generation(NEW)
    old = rig.generation(OLD)
    res = rig.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert "kickstart" in rig.calls()
    assert not old.exists()


def test_cleanup_never_invoked_when_only_selected_generation_exists(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    rig.generation(NEW)
    res = rig.run()
    assert res.returncode == 0, res.stderr
    assert rig.calls("gc_calls") == ""


def test_no_gc_flag_leaves_old_generations(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    rig.generation(NEW)
    old = rig.generation(OLD)
    res = rig.run("--no-gc")
    assert res.returncode == 0, res.stderr
    assert old.exists()
    assert rig.calls("gc_calls") == ""


def test_watch_mode_is_silent_about_generations_still_in_use(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    rig.generation(NEW)
    rig.generation(OLD, leased=True)
    res = rig.run("--watch", "--settle", "0")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "" and res.stderr == ""
    assert (rig.tmp_path / "hermes_home" / "installs" / "0123456789abcdef" / "environments" / OLD).exists()


def test_watch_mode_logs_a_removal(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    rig.generation(NEW)
    rig.generation(OLD)
    res = rig.run("--watch", "--settle", "0")
    assert res.returncode == 0, res.stderr
    assert f"removed unused old generation {OLD}" in res.stdout


def test_cleanup_failure_is_a_warning_not_a_failure(rig, monkeypatch):
    monkeypatch.setenv("FAKE_GC_FAIL", "1")
    rig.select(NEW)
    rig.spoke(1000, NEW)
    rig.generation(NEW)
    old = rig.generation(OLD)
    res = rig.run()
    assert res.returncode == 0
    assert "cleanup of old generations failed" in res.stderr
    assert "simulated collector failure" in res.stderr
    assert old.exists()


# --- waiting for Hermes to be idle, and re-checking the selection --------------


def _hold_lock(path: Path):
    import fcntl
    path.touch()
    f = open(path, "r+")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def test_waits_while_hermes_install_lock_is_held(rig):
    rig.select(NEW)
    rig.spoke(1000, NEW)
    holder = _hold_lock(rig.install_lock_path())
    try:
        t0 = time.time()
        res = rig.run(env_extra={"HERMES_POST_UPDATE_BUSY_MAX": "4"})
        elapsed = time.time() - t0
    finally:
        holder.close()
    assert res.returncode == 0, res.stderr
    assert elapsed >= 3.5
    assert "waiting for a Hermes dependency install to finish" in res.stdout
    assert "still installing/changing after 4s" in res.stderr


def test_proceeds_as_soon_as_install_lock_is_released(rig):
    import threading
    rig.select(NEW)
    rig.spoke(1000, OLD)
    holder = _hold_lock(rig.install_lock_path())
    threading.Timer(3, holder.close).start()
    t0 = time.time()
    res = rig.run()
    elapsed = time.time() - t0
    assert res.returncode == 0, res.stdout + res.stderr
    assert 2.5 <= elapsed < 30
    assert "kickstart" in rig.calls()
    assert "still installing" not in res.stderr


def test_watch_settle_also_waits_for_a_fresh_generation_dir(rig):
    rig.select(NEW)              # facts.json is an hour old...
    rig.spoke(1000, NEW)
    rig.generation(NEW)          # ...but environments/ just changed (build started)
    t0 = time.time()
    res = rig.run("--watch", "--settle", "3")
    assert res.returncode == 0, res.stderr
    assert time.time() - t0 >= 2.5


def test_selection_change_during_run_triggers_another_pass(rig):
    third = "d" * 32
    rig.select(NEW)
    rig.spoke(1000, NEW, restart_env=third)
    rig.flip_selection_mid_run(third)   # Hermes switches facts.json while we run
    res = rig.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"selected environment changed to {third} during this run; checking again" in res.stdout
    assert "kickstart" in rig.calls()
    assert f"spoke pid 1001 is on {third}" in res.stdout
    # The second pass must not re-run `hermes --version` / `hermes update`.
    assert rig.calls("hermes_calls").count("--version") == 1
