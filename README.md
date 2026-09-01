# hermes-hub

A WebSocket hub-and-spoke A2A peer protocol for Hermes instances. Only the
hub binds a listening socket; every Hermes instance (including the hub's own
machine) connects outbound as a pure WebSocket client ("spoke"), so
IT-managed/firewalled machines can participate in the peer network without
any inbound firewall exception. See
`.hermes/plans/2026-09-01_000000-websocket-hub-spoke-protocol.md` for the
full design and decision record.
