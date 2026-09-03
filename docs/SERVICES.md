# Hermes Hub managed services (V10)

V10 M1–M3 installs two user LaunchAgents on Pumpkin:

| Label | Purpose | Runtime |
|---|---|---|
| `ai.hermes.hub` | Hub listener on `127.0.0.1:8770` | `/Users/mtwomey/Git_Repos/hermes-hub/.venv` |
| `ai.hermes.spoke` | Pumpkin's outbound local Hermes spoke | existing `~/.hermes/hermes-agent/venv` — **no package is installed by this service** |

The installer is `services/install-hub-services.sh`. It owns only these two labels. It must never target `ai.hermes.gateway`.

## Local deployment configuration

The portable default is loopback. A hub host that accepts LAN spokes keeps its
non-secret endpoint settings in `~/.config/hermes-hub/service.env`, created by
copying `services/hub-service.env.example` and setting the reachable public
URL. Every `install`, `reinstall`, and `status` invocation reads this file;
environment variables remain one-command overrides.

```bash
cp services/hub-service.env.example ~/.config/hermes-hub/service.env
# Edit HUB_PUBLIC_URL for this deployment, then:
services/install-hub-services.sh reinstall
```

With `HUB_HOST=0.0.0.0`, the installer rejects a missing `HUB_PUBLIC_URL`;
it never silently regenerates a loopback-only plist. Credentials remain in the
Keychain and do not belong in this file.

## M4: verified after reboot

**M4 passed on 2026-09-01.** After Matthew rebooted Pumpkin, both user LaunchAgents started without terminal intervention, the hub listened on `127.0.0.1:8770`, `/health` listed `Pumpkin`, and the installed `peer_list` model tool returned Pumpkin. Evidence: `.hermes/evidence/v10-m4-cold-boot.md`.

For future diagnosis after a reboot or logout/login:

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

## Verified M4 behavior

M1–M4 prove supervised runtime operation, explicit hub/spoke kill-and-recovery, Keychain posture, operation after the hand-started foreground processes were ended, and automatic startup/reconnection across Matthew's reboot. See `.hermes/evidence/v10-m4-cold-boot.md`.

Normal operations still require the Aqua user session to be available: these are user LaunchAgents, not system daemons. Future changes to LaunchAgent policy, credential access, or operating-system behavior require rerunning the diagnostic checklist above.
