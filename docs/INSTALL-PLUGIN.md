# Installing the hermes-hub-peer plugin (W3 M3)

**Audience:** Matthew, running this by hand.
**Why by hand:** installing into `~/.hermes/plugins/` and restarting the
gateway modifies *the runtime executing the session*. An autonomous session
that restarts the gateway terminates itself mid-task, so M3 is deliberately
reserved. M1 and M2 are done and committed; everything below is what remains.

Each step says what a passing result looks like. Steps marked
**[UNVERIFIED]** could not be exercised without the live gateway — they are
reasoned from the plugin loader's source and from the isolated-runtime proof,
not from a real restart.

---

## 0. Before you start

| Fact | Value |
|---|---|
| Repo | `/Users/mtwomey/Git_Repos/hermes-hub` |
| Plugin source | `<repo>/plugin/hermes_hub_peer/` |
| Hub port | 8770 |
| Live gateway's hermes-peer listener | 8765 — leave alone |
| Hermes venv | `~/.hermes/hermes-agent/venv` — **nothing gets installed into it** |

Sanity check that nothing is already there:

```bash
ls -la ~/.hermes/plugins/ | grep hermes-hub-peer || echo "not installed (expected)"
lsof -nP -iTCP:8770 -sTCP:LISTEN || echo "8770 free"
```

---

## 1. Install the plugin

A symlink, not a copy — the plugin locates the `hermes_hub` package by
walking up from its own `__file__` to the repo root, so the checkout stays
the single source of truth and `git pull` updates the plugin.

```bash
ln -s /Users/mtwomey/Git_Repos/hermes-hub/plugin/hermes_hub_peer \
      ~/.hermes/plugins/hermes-hub-peer
```

**Pass:** `ls -l ~/.hermes/plugins/hermes-hub-peer` shows the symlink
resolving into the repo.

> If you ever copy the directory elsewhere instead of symlinking, set
> `HERMES_HUB_REPO` to the hermes-hub checkout so the package can still be
> found.

### Enable it

User plugins are opt-in via `plugins.enabled`:

```bash
hermes plugins enable hermes-hub-peer
```

**Pass:** `hermes plugins list` shows `hermes-hub-peer` as enabled.
**[UNVERIFIED]** — the CLI path was not run; the isolated proof enabled the
plugin by writing `plugins.enabled` into a temp `config.yaml` directly, which
is the same config key the CLI edits.

---

## 2. Configure the hub URL (required — the tools stay hidden without it)

The six tools carry a `check_fn` that hides them entirely unless a hub is
configured. This is intentional: an unconfigured install costs nothing in
the tool schema.

```bash
mkdir -p ~/.hermes-hub
cat > ~/.hermes-hub/config.json <<'JSON'
{
  "hub_url": "http://127.0.0.1:8770",
  "hub_token": ""
}
JSON
```

`HERMES_HUB_URL` in the environment works too and takes precedence.

**Pass:** after the restart in §4, `peer_list` is visible to the model. If
the tools do not appear, this file is the first thing to check.

### Per-spoke credentials (V5a) — only needed for spokes that enforce one

Caller side (this machine), for a spoke named `Olive`:

```bash
security add-generic-password -s hermes-hub -a 'caller:Olive:credential' -w
```

Spoke side (the other machine), the **same** secret:

```bash
security add-generic-password -s hermes-hub -a 'spoke:Olive:credential' -w
```

Placing both by hand is deliberate (VISION.md open question 6 — do not build
tooling for this; V5b retires the problem). A spoke with no secret configured
runs in dev mode and accepts anything, which is fine on a trusted LAN but is
not the intended end state.

---

## 3. Start the hub

The hub is a **separate process** from the gateway. Right now it must be
started by hand:

```bash
cd /Users/mtwomey/Git_Repos/hermes-hub
.venv/bin/python scripts/run_hub.py 8770
```

and a spoke, in another terminal:

```bash
cd /Users/mtwomey/Git_Repos/hermes-hub
HERMES_HUB_SPOKE_CREDENTIAL='<the spoke secret>' \
  ~/.hermes/hermes-agent/venv/bin/python scripts/real_spoke.py 8770 Pumpkin
```

**Pass:** `curl -s http://127.0.0.1:8770/health` returns
`{"status":"ok","connected_spokes":["Pumpkin"]}`.

### Honest note on V10 — this plan does NOT achieve it

V10 wants Pumpkin's own Hermes auto-connected as a spoke with nothing
hand-started, and W3's workstream description wants the hub running as a
managed service rather than a foreground process. **Neither is delivered
here.** Today:

- the hub is a foreground `scripts/run_hub.py` process
- the local spoke is a foreground `scripts/real_spoke.py` process
- both die when their terminal closes, and nothing restarts them

So the vision sentence works only while those two processes happen to be up.
Making them launchd services (and auto-connecting the local Hermes as a
spoke) is real remaining work — it is not part of W3's M1/M2 scope and was
not built. Track it as a follow-up before calling V10 done.

---

## 4. Restart the gateway

Plugin code loads at startup; there is no hot reload.

```bash
hermes gateway restart
```

This restarts the process serving your desktop sessions. Prefer
`gateway restart` over killing the PID: on a launchd-supervised install it
drains in-flight work and is self-healing.

**Pass:**

```bash
hermes gateway status          # alive, new PID
```

Then tail the log for the plugin's own line rather than trusting the exit
code — a live PID with no load line usually means the plugin raised inside a
broad `except` during init:

```bash
grep -i "hermes-hub-peer" ~/.hermes/logs/agent.log | tail -20
```

**[UNVERIFIED]** — the exact log wording after a real gateway restart. The
plugin logs at `debug`; set `HERMES_PLUGINS_DEBUG=1` before the restart if
nothing appears.

---

## 5. Verify in a real session

In the Hermes desktop app, in a **new** conversation:

1. **Tools present** — ask "list my Hermes peers". The model should call
   `peer_list`.
   **Pass:** a JSON result naming `Pumpkin` with its skill description.
   If the model says it has no such tool, the `check_fn` is returning false —
   recheck §2.

2. **A real answer from the peer** — "ask Pumpkin what its hostname is".
   **Pass:** `peer_ask` returns the peer's real answer. Verified
   out-of-process during Gate 2 through the registered handler:
   `The hostname of this machine is 'flavus'. GATE2-E2E-OK`.

3. **A file comes back** — "ask Pumpkin to write a file containing the
   current date, then fetch it".
   **Pass:** `peer_fetch_artifact` writes the file locally and reports a
   sha256. Verified during Gate 1: `b'W3 GATE 1 LIVE'`, byte-identical.

**[UNVERIFIED]** — that the *model* chooses these tools unprompted from their
descriptions. The handlers and schemas are proven; tool-selection behaviour
in a live session is exactly what M3 exists to find out.

---

## 6. Rollback

Nothing to revert in Hermes core — the plugin never wrote there, there is no
`.pth` file, and no package was installed into the runtime venv. Rollback is
therefore just removing the plugin:

```bash
hermes plugins disable hermes-hub-peer
rm ~/.hermes/plugins/hermes-hub-peer     # removes the SYMLINK only
hermes gateway restart
```

**Pass:** `hermes plugins list` no longer shows it; the `peer_*` tools are
gone from a new session.

Faster partial rollback, no restart needed for the *next* session's tool
visibility: delete `~/.hermes-hub/config.json` and unset `HERMES_HUB_URL`.
The `check_fn` then hides the tools. The plugin is still loaded, so this is a
mitigation, not an uninstall.

Optional cleanup of plugin-written state (safe to keep):

```bash
rm -f ~/.hermes-hub/discovered-peers.json   # cached spoke skills, no secrets
```

Keychain entries are not touched by uninstall; remove them explicitly if you
want them gone:

```bash
security delete-generic-password -s hermes-hub -a 'caller:Olive:credential'
```

### If the gateway will not come back

`hermes gateway restart` is launchd-supervised and self-healing, but if a
session is lost mid-restart, check `hermes gateway status` and the log first.
To rule the plugin out as the cause, remove the symlink and restart again —
with the symlink gone, Hermes is in exactly its pre-install state.

---

## 7. What was verified before this runbook

For provenance, from `.hermes/evidence/`:

| Property | Where |
|---|---|
| Six handlers work live against a real hub + real Hermes spoke | `w3-gate1.md` §4.3 |
| Wrong credential rejected before agent invocation | `w3-gate1.md` §4.5 |
| Credential leaks into no output | `w3-gate1.md` §4.6 |
| Plugin loads in Hermes's real PluginManager, six tools in the real registry | `w3-gate2.md` §3 |
| `check_fn` hides/shows correctly inside the real runtime | `w3-gate2.md` §3 |
| End-to-end `peer_ask` through the **registered** handler | `w3-gate2.md` §4 |
| System prompt byte-identical with the spoke connected vs disconnected (V3) | `w3-gate2.md` §5 |
| Live runtime and `~/.hermes/plugins/` untouched throughout | `w3-gate2.md` §6 |
