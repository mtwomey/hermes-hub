# Olive M4 reboot-survival runbook

M4 was performed by Matthew and **passed**: after Pumpkin and Olive were rebooted normally, both spokes reappeared automatically and a post-reboot authenticated remote task reached Olive. Evidence: `.hermes/evidence/w4-m4-reboot-survival.md`.

This runbook remains the recovery procedure for future reboots.

## Preconditions

- W4 Gate 3 has passed; see `.hermes/evidence/w4-m3-real-task.md`.
- No one manually starts `scripts/real_spoke.py` before or after the reboot.
- Do not change Olive firewall, Jamf, CyberArk, or create an Olive listener. Olive must remain outbound-only.
- The SSH reverse tunnel may be used to inspect Olive after it returns, but it is not hub transport.

## Procedure

1. Reboot **Pumpkin** and **Olive** normally. Allow both user sessions to finish login/startup.
2. On Pumpkin, wait for the supervised hub to return:

   ```sh
   launchctl print gui/$(id -u)/ai.hermes.hub
   lsof -nP -iTCP:8770 -sTCP:LISTEN
   ```

   Passing result: `ai.hermes.hub` is running and the hub listens on port 8770.
3. On Olive (using the established reverse SSH setup path if necessary), inspect only:

   ```sh
   launchctl print gui/$(id -u)/ai.hermes.spoke
   lsof -nP -iTCP -sTCP:ESTABLISHED
   ```

   Passing result: `ai.hermes.spoke` is launchd-managed and has an established **outbound** connection from Olive's LAN address to Pumpkin `:8770`. There must be no Olive listening socket.
4. In a normal Hermes desktop conversation on Pumpkin, ask: “What Hermes peer machines are available?”

   Passing result: `peer_list` reports both `Olive` and `Pumpkin` without manual spoke startup.
5. Send one minimal authenticated remote request in that same normal conversation: “Ask Olive to report her hostname and logged-in user.”

   Passing result: the answer identifies Olive's hostname and `mattwo01`; Olive's spoke log contains `credential accepted, invoking agent` for that task.

## If Olive does not return

1. **Do not** open or alter firewall/Jamf/CyberArk settings, and do not add a listener.
2. Inspect service state and logs:

   ```sh
   launchctl print gui/$(id -u)/ai.hermes.spoke
   python3 - <<'PY'
from pathlib import Path
for name in ("ai.hermes.spoke.log", "ai.hermes.spoke.error.log"):
    p = Path.home() / ".hermes" / "logs" / name
    print(f"--- {p} ---")
    print(p.read_text(errors="replace") if p.exists() else "missing")
PY
   ```

3. Check Keychain presence only; do not print its value:

   ```sh
   security find-generic-password -s hermes-hub -a 'spoke:Olive:credential' >/dev/null
   echo $?
   ```

   Passing result is `0`.
4. Verify that the LaunchAgent retains the expected non-secret configuration: local repo, Olive spoke name, and Pumpkin LAN host/port 8770. Do not put credentials in the plist.
5. If launchd has the job loaded but it did not start, perform **one** genuine recovery attempt:

   ```sh
   launchctl kickstart -k gui/$(id -u)/ai.hermes.spoke
   ```

   Re-check the logs and direct LAN connection. If it does not remain up after this single attempt, boot it out, preserve the logs, and stop rather than leaving a crash loop on the managed laptop.

A passing M4 result requires both spokes in `peer_list`, a direct Olive→Pumpkin `:8770` connection, and a successful authenticated remote task after both reboots—without anything hand-started.
