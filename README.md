# hermes-hub

A WebSocket hub-and-spoke A2A peer protocol for Hermes instances. Only the
hub binds a listening socket; every Hermes instance (including the hub's own
machine) connects outbound as a pure WebSocket client ("spoke"), so
IT-managed/firewalled machines can participate in the peer network without
any inbound firewall exception. See
`.hermes/plans/2026-09-01_000000-websocket-hub-spoke-protocol.md` for the
full design and decision record.

Non-Hermes agents on the hub's Mac can call it too. See
[`docs/CALLERS.md`](docs/CALLERS.md): give them the hub token's Keychain
location and the agent card URL, and the card covers the rest.

## Operations

- Services (hub, spoke, post-update watcher): [`docs/SERVICES.md`](docs/SERVICES.md),
  setup per host type in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
- **After `hermes update`:** the `ai.hermes.post-update` watcher restarts the
  spoke onto Hermes's new dependency environment automatically. See
  [`docs/POST-UPDATE.md`](docs/POST-UPDATE.md).
- Past failures and their fixes: [`docs/INCIDENTS.md`](docs/INCIDENTS.md).
