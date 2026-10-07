# Keeping the spoke on Hermes's current environment after an update

`ai.hermes.post-update` is a small launchd watcher, installed with the spoke.
After any `hermes update` (or anything else that makes Hermes switch its Python
dependency environment) it restarts `ai.hermes.spoke` onto the new environment
automatically. It doesn't change anything else.

If you only read one thing: **after `hermes update` you no longer need to
restart the spoke by hand.** Within about a minute and a half the watcher does
it, waits for any running peer task to finish first, checks the result, and
posts a macOS notification. The gateway and desktop app are still yours to
restart (the update normally does the gateway).

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
the old generation until it's restarted. Hermes deletes an unused old
generation only after its last user exits and it is more than 24 h old, so a
spoke that's never restarted also keeps ~730 MB of stale environment on disk.

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

Safety properties:

- **It never runs `hermes`** in `--watch` mode, so it can't itself trigger a
  dependency rebuild or interfere with a running update.
- **It never restarts blind.** If it can't tell which generation the spoke uses
  (nothing open under `environments/` yet), it does nothing.
- **One instance at a time**: lock at `~/.hermes/locks/hermes-post-update.lock`;
  a lock left by a dead process is reclaimed.
- It touches only `ai.hermes.spoke`. Never the hub, never the gateway, never
  Hermes's environments, never credentials.

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
the update left pending, then does steps 3–6 with no settle delay and a 10-minute
drain. Flags: `--force` (restart even if current), `--no-wait` (don't wait for
in-flight tasks), `--wait N`, `--settle N`, `--watch`.

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

Notifications (title **Hermes post-update**) appear only when it acts, postpones,
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
so they leave the old generation too; Hermes garbage-collects it after 24 h.
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
