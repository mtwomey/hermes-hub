# Keeping the spoke on Hermes's current environment after an update

`ai.hermes.post-update` is a small launchd watcher, installed with the spoke.
After any `hermes update` (or anything else that makes Hermes switch its Python
dependency environment) it restarts `ai.hermes.spoke` onto the new environment
automatically, and deletes old environments once nothing uses them any more.
It doesn't change anything else.

If you only read one thing: **after `hermes update` you no longer need to
restart the spoke by hand.** Within about a minute and a half the watcher does
it, waits for any running peer task to finish first, checks the result, and
posts a macOS notification. The gateway and desktop app are still yours to
restart (the update normally does the gateway). Once they've restarted, the
old ~730 MB environment is deleted automatically, usually within seconds and
at most 15 minutes later.

## Why this exists

Hermes no longer has a single venv. It keeps its Python dependencies in
content-hashed *generations*:

```
~/.hermes/installs/<install-id>/environments/<generation>/venv   # one per build
~/.hermes/installs/<install-id>/facts.json                       # which one is selected
```

`hermes update`, `hermes pm repair`, and editing a plugin repo that Hermes
fingerprints each build a new generation and select it in `facts.json`. Every
Hermes process picks the selected generation **when it starts**:

| Process | How it finds the environment | Restarted by the update? |
|---|---|---|
| Gateway (`ai.hermes.gateway`) | Hermes launcher + `hermes_bootstrap` | Yes |
| Desktop app | Hermes launcher + `hermes_bootstrap` | Relaunch the app |
| Spoke (`ai.hermes.spoke`) | `hermes-spoke-wrapper.sh` reads Hermes's launcher, `scripts/real_spoke.py` imports `hermes_bootstrap` | **No.** That's what this watcher fixes |
| Hub (`ai.hermes.hub`) | This repo's own `.venv` (VISION V8: no Hermes dependency) | Not affected |

Nothing pins a generation path; that would break on the next update (see
`INCIDENTS.md`, 2026-09-28). So a spoke started before an update keeps running
the old generation until it's restarted.

Hermes's own cleanup doesn't help much either. Its garbage collector runs only
right after `hermes update` publishes a new generation
(`collect_superseded_generations` in `hermes_cli/venv_sync.py`), or when you run
`hermes pm gc`. It skips any generation a running process still uses, or that
is under 24 h old. At that moment the old generation is almost always still in
use (nothing has restarted yet), so it's skipped, and nothing looks again until
the *next* update. Each update therefore left ~730 MB behind indefinitely.

## How it works

`services/hermes-post-update.sh --watch`, run by launchd:

1. **Triggers.** `WatchPaths` on `~/.hermes/installs/<install-id>/` (the directory
   `facts.json` is written into), plus `StartInterval` 900 s as a safety net and
   once at login (`RunAtLoad`). Other Hermes housekeeping in that directory also
   fires it; those runs are cheap silent no-ops.
2. **Settles.** Waits until `facts.json` has been unchanged for 60 s, so it never
   acts in the middle of an update.
3. **Compares.** Reads the selected generation from `facts.json` and the
   generation the spoke process actually has open (`lsof`). If they match, it
   exits silently. That's what happens on almost every run.
4. **Drains.** Waits up to 5 min for in-flight spoke tasks to finish: rows in
   `~/.hermes-hub/spoke_ledger.db` with state `working`/`submitted`, ignoring rows
   older than the hub task TTL (`HERMES_HUB_TASK_TTL_SECONDS`, 1800 s), which
   belong to a spoke that died mid-task. If tasks are still running it gives up
   for now (exit 2) and the next 15-minute run tries again.
5. **Restarts and verifies.** `launchctl kickstart -k gui/<uid>/ai.hermes.spoke`,
   then confirms the new spoke process has the selected generation open and that
   a new `connected and registered` line appeared in the spoke log.
6. **Reports.** Warns about any other Hermes process still on an older generation
   (typically the desktop app until you relaunch it) and posts a notification.
7. **Cleans up (every run, even when the spoke was already current).** If any
   generation other than the selected one exists, it runs Hermes's own collector
   (`hermes_cli.runtime_state.collect_generations`, the same code as
   `hermes pm gc`), imported directly so no Hermes startup runs. The collector
   deletes only generations that are not selected and that no running process
   holds a lease on. It takes Hermes's install lock for at most 10 s and skips
   the run if an install holds it. Hermes's extra 24 h minimum age is **not**
   applied (`HERMES_POST_UPDATE_GC_MIN_AGE`, default `0`): the leases are the
   real guard. Each removal is logged with its size and announced in a
   notification. A generation still in use is kept silently and removed on the
   first run after its last user exits. Restarting the gateway or desktop app
   usually triggers that run within seconds, at most 15 min later. The small
   package-manager runtime generations (`pm-runtime/`) are collected the same way.

Safety properties:

- **It never runs `hermes`** in `--watch` mode, so it can't itself trigger a
  dependency rebuild or interfere with a running update.
- **It never restarts blind.** If it can't tell which generation the spoke uses
  (nothing open under `environments/` yet), it does nothing.
- **One instance at a time**: lock at `~/.hermes/locks/hermes-post-update.lock`;
  a lock left by a dead process is reclaimed.
- It restarts only `ai.hermes.spoke`: never the hub, never the gateway, never
  credentials. The only thing it deletes is old dependency generations, and only
  through Hermes's own collector, which refuses anything selected or in use.
  `--no-gc` (or `HERMES_POST_UPDATE_GC_MIN_AGE=86400` to keep Hermes's 24 h rule)
  turns that down.

## Install

The watcher is installed automatically by `install` / `reinstall` in `spoke` or
`both` mode. On a host whose hub/spoke services are already running, install
**only the watcher**. This never regenerates or restarts the hub or spoke
plists, so it can't hit the LAN-bind pitfall in `SERVICES.md`:

```bash
cd ~/Git_Repos/hermes-hub
git pull
services/install-hub-services.sh install-watcher
```

This writes `~/Library/LaunchAgents/ai.hermes.post-update.plist`, loads it
(idempotent: re-running reloads it), and links
`~/.local/bin/hermes-post-update` to the script for manual use. The install-time
run is a silent no-op when the spoke is already current.

Check it:

```bash
services/install-hub-services.sh status          # lists ai.hermes.post-update + last activity
launchctl print gui/$(id -u)/ai.hermes.post-update | grep -E 'state|runs|last exit'
plutil -p ~/Library/LaunchAgents/ai.hermes.post-update.plist | grep -A2 WatchPaths
```

Expect `last exit code = 0` and `WatchPaths` naming your
`~/.hermes/installs/<install-id>` directory. If Hermes creates a *new*
install-id directory (a fresh checkout location), run `install-watcher` again so
it watches the new one; the 15-minute run covers the gap meanwhile.

Remove it (leaves hub and spoke alone):

```bash
services/install-hub-services.sh uninstall-watcher
```

## Manual use

```bash
hermes-post-update            # after `hermes update`
hermes-post-update --update   # run `hermes update`, then this
hermes-post-update --help
```

Manual mode first runs `hermes --version`, which finishes any dependency build
the update left pending, then does steps 3–7 with no settle delay and a 10-minute
drain. Manual mode also names any old generation it had to keep because a
process still uses it. Flags: `--force` (restart even if current), `--no-wait`
(don't wait for in-flight tasks), `--no-gc` (skip cleanup), `--wait N`,
`--settle N`, `--watch`.

Exit codes: `0` done or nothing to do, `1` failure, `2` in-flight tasks didn't
finish in time (spoke left untouched).

## Logs and notifications

| What | Where |
|---|---|
| Actions taken | `~/.hermes/logs/ai.hermes.post-update.log` |
| Warnings / failures | `~/.hermes/logs/ai.hermes.post-update.error.log` |
| Spoke's own startup | `~/.hermes/logs/ai.hermes.spoke.error.log` |

A no-op run writes nothing, so the log only grows when something happened. A
successful move looks like:

```
[post-update] selected environment is 7eba115c…; spoke (pid 84189) is on '990041d4…'; restart required
[post-update] restarting ai.hermes.spoke...
[post-update] spoke pid 27227 is on 7eba115c…
[post-update] spoke re-registered with the hub
```

and a cleanup, once the last process has left the old generation:

```
[post-update] removed unused old generation 7eba115c… (727 MB)
```

Notifications (title **Hermes post-update**) appear only when it acts, postpones,
removes old environments ("Removed 1 old Hermes environment(s), freed 0.7 GB"),
or fails. "Gateway/desktop still on an old environment: restart them" means
the spoke is fine and something else still needs a restart: `hermes gateway
restart`, or quit and reopen the desktop app.

## Testing it without a Hermes update

`hermes pm repair` rebuilds the dependency environment from the recorded lock
(same packages) into a **new** generation and selects it, which is the same
event an update produces. It's the way to rehearse:

```bash
grep -o 'environments/[0-9a-f]*' ~/.hermes/installs/*/facts.json   # before
hermes pm repair                                                   # ~10 s-2 min
sleep 120
cat ~/.hermes/logs/ai.hermes.post-update.log                       # watcher moved the spoke
P=$(launchctl list | awk '$3=="ai.hermes.spoke"{print $1}')
lsof -p "$P" | grep -oE 'environments/[0-9a-f]+' | sort -u        # == new selection
```

Then restart the gateway (`hermes gateway restart`) and relaunch the desktop app
so they leave the old generation too. The watcher's next run (usually triggered
by that restart) deletes it and logs `removed unused old generation …`.
Finish with a routed task to the spoke (e.g. `peer_ask` "reply with hostname"):
registration alone isn't proof (see `INCIDENTS.md`).

Automated tests: `tests/test_post_update.py` (script behaviour against fake
`launchctl`/`lsof`/ledger) and `tests/test_service_definitions.py` (installer
and plist).

## Troubleshooting

| Symptom | Check |
|---|---|
| Spoke still on old generation after an update | `launchctl print gui/$(id -u)/ai.hermes.post-update` (loaded? `last exit code`?), then both logs above. Run `hermes-post-update` by hand to see each step. |
| "postponed: a hub task is still running" | Expected while a peer task runs; it retries every 15 min. Force it with `hermes-post-update --no-wait`. |
| "cannot tell which environment spoke pid … uses" | The spoke started seconds ago or is unhealthy. Check `ai.hermes.spoke.error.log`; the next run retries. |
| "spoke pid N loaded 'X', expected Y" | The spoke resolved a different interpreter/generation. Check `HERMES_SPOKE_PYTHON` overrides in the spoke plist and `INCIDENTS.md` (2026-09-28). |
| Watcher never fires | `WatchPaths` must name the directory that contains the live `facts.json`; re-run `install-watcher`. |
| Old generation not deleted | Something still uses it: `hermes-post-update` names what it kept; `lsof \| grep environments/<hash>` shows who. Relaunch that process. |
| "cleanup of old generations failed" | Logged with the collector's last error line; nothing was deleted. `hermes pm gc` is the manual fallback. |

## Verified

- **Pumpkin, 2026-10-07.** Installed with `install-watcher` (hub and spoke PIDs
  unchanged). `hermes pm repair` selected a new generation at 17:16:44; launchd fired the
  watcher, which settled, restarted the spoke at 17:17:46 and verified it on the
  new generation and re-registered at 17:17:48, and flagged the gateway and
  desktop as still on the old one. A routed `peer_ask` to Pumpkin then
  succeeded. Through 17:32 launchd ran the watcher 8 times (load, the
  generation change, further changes in the watched directory, and the 17:31
  timer run); every run after the restart was a silent no-op with exit 0.
- **Olive, 2026-10-07.** `git pull` to `0e5f09b`, `tests/test_post_update.py` +
  `tests/test_service_definitions.py` passed on Olive (36), `install-watcher`
  left the spoke PID unchanged, manual `hermes-post-update` correctly did
  nothing. Rehearsal: `hermes pm repair` run *inside* a hub task selected a new
  generation at 17:36:34; the spoke ledger showed that task as `working`, the
  watcher waited for it to finish, restarted the spoke at 17:37:36, verified it
  on the new generation and re-registered at 17:37:39. The next routed task was
  served by the new spoke process.
- **Cleanup, Pumpkin, 2026-10-07.** Manual run with the real collector kept
  `990041d4…` (held by the desktop app) and removed nothing. A further
  `hermes pm repair` moved the spoke automatically (17:55:10). The old
  `7eba115c…` was kept while the gateway still held it. `hermes gateway restart`
  at 17:56:13 freed it, and the watcher deleted it at 17:56:32 (727 MB) with no
  manual step.
