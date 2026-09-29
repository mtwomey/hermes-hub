# Incident log

Post-incident notes for hub/spoke connectivity problems, kept so the next
occurrence (on either machine) starts from evidence instead of guessing.

---

## 2026-09-04: Olive spoke silently deregistered from hub (split-brain)

**Reported by:** Matt, from Olive's own session. Diagnosed live; not
reproduced on demand.

### Symptom

- `peer_list` from Olive showed only **Pumpkin** — Olive was missing from
  its own view of the hub's peer roster.
- Olive's spoke process (`ai.hermes.spoke`, `scripts/real_spoke.py`) was
  running the whole time, with no crash. Its own log showed periodic
  `connection attempt failed: timed out during opening handshake` lines
  followed by `connected and registered`, in a flapping pattern.
- The flapping in the log was a red herring / already resolved by the time
  it was investigated: the log had stopped growing ~16 minutes before the
  investigation started (last line at 09:33:56, checked repeatedly up to
  09:50 with zero new lines). The *actual* live problem was silent, not
  noisy.

### Investigation, false leads ruled out (in order tried)

1. **Network path / DHCP / firewall to the hub.** `ping`, `nc -zv` on
   8770 and 22, and a `curl` to `/.well-known/agent-card.json` (got the
   expected 401) all succeeded cleanly and quickly. Ruled out.
2. **Cisco AnyConnect VPN interference.** The box has AnyConnect installed
   and several `utun` interfaces up, which looked suspicious. Checked with
   `vpn -s stats`: AnyConnect itself was **Disconnected**, 0 bytes/session
   duration. The real VPN in use was an unrelated IKEv2 tunnel
   (`CommCenter` isakmp/ipsec-msft, MDM-managed). Ruled out — and this was
   a bad theory to begin with: fast, uncontended one-shot `curl`/`nc`
   checks don't validate steady-state connection behavior either way.
3. **CPU starvation on Olive's machine delaying the WS handshake past a
   default `open_timeout`.** Load average was genuinely high (Defender,
   Jamf, CyberArk EPM, WindowServer all busy). Looked plausible, but was
   directly disproved by reproducing the actual handshake: a small script
   using the same `hermes_hub.spoke_client` code path connected to the
   *real* hub 5/5 times in under 200ms each, with the machine in the same
   loaded state. Ruled out by direct measurement, not by argument.
4. **The obvious "is the hub even up" check.** Confirmed via `peer_ask` to
   Pumpkin (same host as the hub) that the hub process itself had been
   running continuously for ~37 hours with zero crashes, low resource
   usage, and — critically — its own log showed **46 clean, error-free
   WebSocket accepts from Olive's IP** with no rejections, throttling, or
   auth failures. The hub was never the thing failing.

### Root cause (confirmed, not theoretical)

A **split-brain between the OS-level TCP socket and the hub's in-memory
application registry**:

- Olive's spoke process still held a TCP socket in `ESTABLISHED` state
  to the hub (confirmed via `lsof`/`netstat`), and its own last log line
  said "connected and registered."
- But asking Pumpkin to check the hub's *live* in-memory spoke registry
  directly (not logs) confirmed the hub did **not** have Olive registered
  at that moment — only itself.
- Neither side's connection-loss detection fired: the client's
  `SpokeClient.run()` reconnect loop only triggers on `ConnectionClosed` /
  `OSError` / `asyncio.TimeoutError` from the socket; the socket never
  raised those, so the client never knew its registration was gone. The
  hub's registry entry is only removed in the `finally` block of
  `spoke_endpoint` when its own `receive_text()` loop raises — which also
  never happened on the hub's side for this connection.
- Likely trigger: a hub process restart/reinstall cycle earlier that
  morning (logs show ~15 restarts before 09:33, all before the process
  that was live during the incident). The registry was presumably wiped
  by that restart while Olive's outbound socket survived it at the OS
  level (or some other event tore down just the hub's server-side
  handling of that one connection without a clean FIN/RST).

**Ping/pong was not the gap.** Checked uvicorn's WebSocket implementation
(`websockets_impl.py` / `websockets_sansio_impl.py`) and the client's
`websockets.connect()` call — both sides use the `websockets` library's
default `ping_interval=20.0s` (uvicorn's `ws_ping_interval` default, not
overridden in `run_hub.py` or `cli.py`; client doesn't override it
either). A working ping/pong loop should have detected a truly dead peer
within ~40s. It didn't, over at least 16+ minutes. That means whatever
broke, broke in a way that bypassed transport-level ping/pong entirely —
most plausibly the specific per-connection asyncio task on the hub side
died/hung without ever completing a close handshake, so no pong ever had
a chance to fail. **This directly informs the fix below: tightening
ping_interval/ping_timeout is not expected to help, since the existing
20s/20s already had ample time to catch a merely-slow connection and
didn't.**

### Important side-observation: outbound vs inbound are independent paths

While Olive's *inbound* registration was dead, `peer_ask` calls **from**
Olive **to** Pumpkin worked the entire time. This is not a contradiction:

- `peer_ask` is Olive's process making an outbound HTTP/JSON-RPC call to
  the hub's external A2A surface, which the hub then routes to Pumpkin's
  *own* independently-healthy spoke connection. It never touches Olive's
  own registration.
- Only an *inbound* task — something routed by the hub *to* Olive — would
  have exposed the break, because that requires the hub to have Olive in
  its live registry.
- Lesson for any future self-check: verifying "can I reach the hub" (a
  plain HTTP call, or even successfully calling `peer_ask` on someone
  else) proves nothing about whether your own inbound registration is
  still alive. The self-check has to exercise the same channel inbound
  tasks would use — the persistent spoke socket itself — not a
  side-channel HTTP request.

### Resolution applied (manual, this incident only)

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.spoke
```

Forced Olive's spoke process to restart, drop the stale socket, and
re-register. Confirmed fixed via `peer_list` showing Olive again (not
just Olive's own log), and independently via `peer_ask` to Pumpkin
checking the hub's live registry.

No code or config was changed to fix this incident — it was a live-state
problem (stale in-memory registry entry vs. a still-open OS socket), not
a persistent bug requiring a deploy. It can recur under the same trigger
(a hub restart racing an existing spoke connection) until a real fix
below lands.

---

### Recommended fix (not yet implemented — pending decision)

**Goal:** detect "my socket looks fine but the hub doesn't actually have
me registered anymore" automatically, and have the spoke self-heal
without a manual `launchctl kickstart`.

**Primary recommendation: application-level heartbeat over the existing
spoke socket, hub-side idle pruning as the complement.**

Rationale for not just tuning ping/pong: the transport-level mechanism
already had 20s/20s and never caught this over 16+ minutes, so there's no
evidence a shorter interval would behave differently — it would only
poll a broken assumption more often. The break was at the *hub's
application-level task/registry* layer, not something a faster transport
ping is guaranteed to observe from the client's side.

Concretely:

1. **Client → hub heartbeat.** In `SpokeClient` (or the executor that owns
   the socket), send a small `{"type": "heartbeat"}` frame on the already
   -open connection every ~5s. Track time since the hub's corresponding
   `{"type": "heartbeat_ack"}` was last received. If no ack arrives within
   a short window (e.g. 3 missed beats / ~15s), treat the connection as
   dead: close the local socket and let the *existing* reconnect-with
   -backoff loop in `SpokeClient.run()` do the rest — no new reconnect
   logic needed, just a new trigger path alongside the existing
   `ConnectionClosed`/`OSError`/`asyncio.TimeoutError` catches.
2. **Hub → client heartbeat ack.** In `hub_server.py`'s `spoke_endpoint`,
   recognize the `heartbeat` frame type in the receive loop and reply with
   `heartbeat_ack` immediately, without forwarding it to `Router`. Cheap,
   symmetric, and proves the *specific connection's own async task* on the
   hub side is still alive and scheduled — which is exactly the thing
   that silently died in this incident.
3. **Hub-side idle pruning (belt-and-suspenders, addresses the other half
   of the split-brain).** Right now the hub only deregisters a spoke
   reactively, when its `receive_text()` loop raises. Add a companion
   check: if a registered connection hasn't sent *anything* (including a
   heartbeat) in some threshold (e.g. 30-45s), have the hub proactively
   close and deregister it, rather than trusting the connection to
   eventually error out on its own. This protects against the mirror
   failure mode — a client that thinks it's fine but the hub's socket
   handling is actually stuck.

**Why this over the alternatives considered:**

- *Just shortening `ping_interval`/`ping_timeout`* — rejected as primary
  fix; already explained above, the existing interval had ample time and
  didn't catch this specific failure mode.
- *Client polling an external HTTP status endpoint on the hub
  (`/.well-known/agent-card.json`) as a self-check* — useful as a
  supplementary signal, but doesn't exercise the actual spoke socket, so
  it can't detect this exact failure (socket up, hub-side task dead) as
  directly as a heartbeat over the same channel the real work travels on.
  Also requires wiring the external bearer token into the spoke process,
  which today only holds the join token.
- *Do nothing, rely on manual restarts* — status quo; works, but requires
  a human (or another peer) to notice and intervene each time, and this
  incident was only caught because Matt asked a direct question at the
  right moment.

**Scope/effort estimate:** small. Roughly:
- `hermes_hub/spoke_client.py`: background heartbeat-send task + last
  -ack-received tracking + a way to signal `run()`'s loop to treat a
  stale heartbeat like a lost connection.
- `hermes_hub/hub_server.py`: recognize+ack `heartbeat` frames in
  `spoke_endpoint`'s receive loop.
- `hermes_hub/registry.py` (or wherever connections are tracked): last
  -seen timestamp per spoke + a periodic sweep task on the hub to prune
  stale entries.
- Tests: extend `tests/test_spoke_client.py` and `tests/test_hub_server.py`
  to cover a connection that goes silent (no heartbeats, socket doesn't
  raise) and assert both sides recover without a process restart.

**Deployment note:** `~/Git_Repos/hermes-hub` has a shared git remote
(`github.com/mtwomey/hermes-hub.git`), so whichever machine implements this
should push a branch and have the other `git fetch origin` + `git merge`.
Checkouts on different machines still drift, so check `git log --oneline -3`
on each before assuming they match.

**Status: not implemented.** Matt asked to hold off on any code changes
for now; this document exists so the next occurrence (or the eventual
fix) starts from this write-up instead of re-diagnosing from scratch.

---

## 2026-09-28: spoke crash-looped on every inbound task after Hermes moved its runtime

### Symptom

Identical *presentation* to the 2026-09-04 split-brain above, but a
completely different cause — worth reading both before concluding:

- The hub correctly listed Olive as connected (`peer_list` showed it) and
  the spoke's socket was `ESTABLISHED`.
- Every task routed TO Olive returned a bare `{"success": false, "error":
  "task failed"}`.
- Outbound `peer_ask` FROM Olive kept working the whole time.
- `~/.hermes/logs/ai.hermes.spoke.error.log` showed a repeating cycle:
  ```
  spoke Olive: connected and registered
  task <id>: credential accepted, invoking agent
  ModuleNotFoundError: No module named 'websockets'   <- process dies
  spoke Olive: connected and registered               <- KeepAlive respawn
  ```

### Root cause (confirmed by direct measurement)

Hermes migrated its own runtime out from under the service:

- **Before:** one venv, `~/.hermes/hermes-agent/venv` (Python 3.11.15,
  created Jul 27). `hermes-peer` had installed `websockets` / `a2a-sdk` /
  `uvicorn` into it.
- **After (Sep 28, 11:48-11:53):** a managed "store" interpreter at
  `~/.hermes/tools/python-3.14.7.../bin/python3`, plus content-hashed
  dependency *generations* under
  `~/.hermes/installs/<hash>/environments/<generation>/venv`. The launcher
  shim `~/.hermes/hermes-agent/.hermes/bin/hermes` was rewritten to point
  at the store interpreter.

`hermes_bootstrap` compares the store interpreter against `sys.executable`
(`hermes_cli/venv_sync.py::prepare_launch`, the check around lines 281-284)
and, on a mismatch, `os.execv`s the process onto the store interpreter with
**`-I`** — isolated mode, which ignores `PYTHONPATH` and the starting venv's
site-packages. The selected generation's packages only join `sys.path` once
`hermes_bootstrap` has been imported.

`scripts/real_spoke.py` imported `hermes_hub.spoke_client` — whose line 22 is
a module-scope `import websockets` — *before* anything activated Hermes. So
the re-exec'd process died in the one window where no dependencies are
wired up yet.

Proof, rather than inference:
```
store python -I -c "import anthropic"                      -> ModuleNotFoundError
store python -I -c "import hermes_bootstrap; import anthropic" -> 0.87.0
```

### Why it masqueraded as a healthy connection

`import websockets` is at module scope; `from run_agent import AIAgent`
(in `spoke_executor.py`) is lazy. So the spoke completed startup and hub
registration *truthfully* — the hub's "connected" status was never wrong —
and only died once real work arrived. launchd `KeepAlive` respawned it,
re-registering each time. A changing PID across `pgrep -fl real_spoke.py`
was the tell.

### False leads worth not repeating

- **"Just restart the service."** Restarting restores registration without
  fixing anything, so it looks briefly successful and destroys the log
  evidence. The first instinct here was wrong.
- **An outbound `peer_ask` as a health check.** Olive→Pumpkin succeeded
  while Olive's own inbound path was dead. Outbound is an HTTP call to the
  hub's external surface routed to the *target's* connection; it never
  touches the caller's registration. (Same trap as 2026-09-04.)
- **"The traceback is historical."** The log looked stale because the crash
  only fires on an inbound task — it can sit silent for hours. Compare the
  log's mtime to `date` before judging.
- **`launchctl kickstart -k` after editing the plist.** It restarts the
  process but does NOT re-read the plist, so an env-var fix silently does
  nothing and the same crash recurs. Use `bootout` + `bootstrap`, and verify
  with `ps eww -o command= -p <pid> | tr ' ' '\n' | grep HERMES_`.

### Resolution applied

1. `scripts/real_spoke.py` imports `hermes_bootstrap` (agent root overridable
   via `HERMES_AGENT_ROOT`) *before* any `hermes_hub` import, so deps resolve
   wherever Hermes currently keeps them.
2. `services/hermes-spoke-wrapper.sh` resolves the interpreter instead of
   pinning a venv: `HERMES_SPOKE_PYTHON` override, then a legacy
   `HERMES_AGENT_VENV` if it still exists, then Hermes's launcher shim
   (which Hermes itself keeps current). Deliberately does **not** pin a
   generation hash — that would recreate this failure in a subtler form.
3. The preflight now checks `websockets` *after* activation, and no longer
   demands `a2a` — `a2a` is imported only by the hub modules
   (`hub_server` / `hub_executor` / `agent_card`), never by the spoke.
4. `HERMES_AGENT_VENV` removed from both machines' spoke plists, so the
   spoke starts *directly* on the store interpreter with no re-exec hop.
5. The abandoned 3.11 venv was deleted (603 MB) — a loud failure is
   preferable to silent drift onto a stale runtime.

### Deliberate decision: the hub keeps its own `.venv`

The hub was NOT moved onto Hermes's environment. It has no Hermes
dependency (it's a pure relay) and its deps are declared in this repo's
`pyproject.toml`. Installing `a2a-sdk[fastapi]==1.1.2` into Hermes's managed
env would be non-durable (hand-installed packages are wiped when Hermes
rebuilds a generation; there is no `a2a` extra) and actively risky — it
pulls fastapi/starlette 0.141.1/1.6.0 against Hermes's 0.133.1/1.3.1, which
could break the gateway and dashboard. A venv is not the problem; a venv
*someone else owns and abandons* is.

### Verified

Both machines' spokes start directly on `python-3.14.7`, PIDs stable, zero
`ModuleNotFoundError`, and an inbound task from Pumpkin to Olive executed a
real shell command and returned its output. Pumpkin's hub kept the same PID
and start time (Sep 2) throughout — untouched.

Note Pumpkin's spoke was **not** crash-looping: it had been running since
Sep 2, i.e. from before the runtime migration, so its bug was latent and the
next restart would have triggered it. Don't take "it's currently working" as
evidence a machine is unaffected.

### Operational notes

- `pgrep -fl real_spoke.py` returns nothing when run from *inside* a spoke
  task — macOS `pgrep` skips its own ancestors. Use `pgrep -afl` there.
- A peer asked to restart its own spoke cannot report the result in the same
  turn (the restart kills the process serving the request). Have it launch a
  detached script that restarts and writes checks to a file, then follow up
  with a second `peer_ask` to read it.

**Status: fixed and deployed on both machines.**
