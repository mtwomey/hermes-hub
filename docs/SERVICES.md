# Hermes Hub managed services (V10)

V10 M1–M3 installs two user LaunchAgents on Pumpkin:

| Label | Purpose | Runtime |
|---|---|---|
| `ai.hermes.hub` | Hub listener on `127.0.0.1:8770` | `/Users/mtwomey/Git_Repos/hermes-hub/.venv` |
| `ai.hermes.spoke` | Pumpkin's outbound local Hermes spoke | existing `~/.hermes/hermes-agent/venv` — **no package is installed by this service** |

The installer is `services/install-hub-services.sh`. It owns only these two labels. It must never target `ai.hermes.gateway`.

## M4: after a reboot or logout/login

M4 is human-only. Once the Aqua login session is available again, run:

```bash
launchctl list | grep -E 'ai\.hermes\.(hub|spoke)'
lsof -nP -iTCP:8770 -sTCP:LISTEN
curl -sS http://127.0.0.1:8770/health
```

Then, in a normal Hermes desktop session, ask an ordinary question that naturally causes a peer discovery, or call `peer_list`. Passing looks like:

- both labels have PIDs;
- port 8770 is owned by `ai.hermes.hub`;
- `/health` includes `"Pumpkin"` in `connected_spokes`;
- `peer_list` returns the Pumpkin spoke without a terminal manually starting hub or spoke.

If any part fails, inspect (never paste credentials):

```bash
tail -100 ~/.hermes/logs/ai.hermes.hub.log
tail -100 ~/.hermes/logs/ai.hermes.hub.error.log
tail -100 ~/.hermes/logs/ai.hermes.spoke.log
tail -100 ~/.hermes/logs/ai.hermes.spoke.error.log
```

Expected startup lines are `ai.hermes.hub startup: ...` and `ai.hermes.spoke startup: ...`; the spoke error log also says `Keychain credential enforcement enabled`.

## Roll back completely

```bash
cd ~/Git_Repos/hermes-hub
services/install-hub-services.sh uninstall
```

This bootouts/removes **only** `ai.hermes.hub` and `ai.hermes.spoke` and their plists. It does not edit or restart `ai.hermes.gateway`, and it does not alter `~/.hermes/hermes-agent/`.

To also remove the two V5a Keychain credentials after confirming no deployment uses them:

```bash
security delete-generic-password -s hermes-hub -a 'spoke:Pumpkin:credential'
security delete-generic-password -s hermes-hub -a 'caller:Pumpkin:credential'
```

## Unverified until M4

M1–M3 prove supervised runtime operation, explicit hub/spoke kill-and-recovery, Keychain posture, and operation after the hand-started foreground processes were ended. They **do not** prove survival across a cold boot, logout/login, Keychain availability immediately after login, or launchd session-policy behavior after reboot. M4 must not be marked passed until Matthew performs the above check.
