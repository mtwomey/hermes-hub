# hermes-hub-peer — native Hermes plugin

Registers the six `peer_*` model tools so a normal Hermes conversation can
reach Hermes agents on other machines through a [hermes-hub](../../README.md)
instance.

| Tool | Purpose |
|---|---|
| `peer_list` | Spokes connected to the hub right now, with rich skill descriptions |
| `peer_info` | One spoke's identity and full skill list |
| `peer_discover` | Refresh a spoke's skills and cache them locally |
| `peer_ask` | Send a request to a named spoke and return its reply |
| `peer_status` | Read one task by id |
| `peer_fetch_artifact` | Download an artifact a spoke produced, SHA-256 verified |

## What this plugin does NOT do

- It does **not** patch Hermes core, and it does **not** install a `.pth`
  file. Registration goes through `PluginContext.register_tool()`, which
  delegates to `tools.registry.register()`. hermes-peer needed a core shim;
  this does not.
- It does **not** install anything into the Hermes runtime venv.
- It does **not** inject peer state into the system prompt (V3). A spoke
  connecting or disconnecting never invalidates a conversation's prompt
  cache. Discovery is an explicit `peer_list` call.
- It does **not** start the hub. The hub is a separate process — see
  [`docs/INSTALL-PLUGIN.md`](../../docs/INSTALL-PLUGIN.md).

## Install

Full runbook, including verification and rollback:
[`docs/INSTALL-PLUGIN.md`](../../docs/INSTALL-PLUGIN.md). In short:

```bash
ln -s /Users/mtwomey/Git_Repos/hermes-hub/plugin/hermes_hub_peer \
      ~/.hermes/plugins/hermes-hub-peer
hermes plugins enable hermes-hub-peer
hermes gateway restart
```

The plugin finds the `hermes_hub` package by walking up from its own
`__file__` to the repo root, so a symlink into `~/.hermes/plugins/` works
with no `pip install`. If you copy the directory somewhere else instead, set
`HERMES_HUB_REPO` to the hermes-hub checkout.

## Uninstall / rollback

```bash
hermes plugins disable hermes-hub-peer     # tools vanish at next restart
rm ~/.hermes/plugins/hermes-hub-peer       # removes the symlink only
hermes gateway restart
```

Removing the symlink deletes nothing in the repo. Because the plugin never
writes to `~/.hermes/hermes-agent/`, uninstall is complete once the symlink
is gone and the gateway has restarted — there is no patch to revert.

## Configuration

Nothing is hardcoded. Every value is resolved at call time.

### Hub location and hub token

| Key | Where | Meaning |
|---|---|---|
| `HERMES_HUB_URL` | env | Hub base URL, e.g. `http://127.0.0.1:8770` |
| `hub_url` | `~/.hermes-hub/config.json` | Same, as a file |
| `HERMES_HUB_TOKEN` | env | Bearer token for the hub's external A2A surface |
| `hub_token` | `~/.hermes-hub/config.json` | Same, as a file |

Resolution order for each: explicit tool argument → environment → config
file → (for the URL) the default `http://127.0.0.1:8770`.

Example `~/.hermes-hub/config.json`:

```json
{
  "hub_url": "http://127.0.0.1:8770",
  "hub_token": ""
}
```

**`check_fn`:** the six tools are hidden from the model unless
`HERMES_HUB_URL` or `hub_url` is set. Setting neither leaves the toolset
invisible, which is the intended "not configured" state.

### Per-spoke credentials (V5a)

Each spoke enforces its own secret; the hub relays it opaquely and never
validates or stores it. Resolution order for spoke `Olive`:

1. explicit `credential` argument to `peer_ask` (not normally used)
2. env `HERMES_HUB_PEER_CREDENTIAL_OLIVE`
3. macOS Keychain, service `hermes-hub`, account `caller:Olive:credential`
4. empty → dev mode (the spoke allows anything if it has no secret either)

Add a Keychain entry:

```bash
security add-generic-password -s hermes-hub -a 'caller:Olive:credential' -w
```

The matching secret must exist on the spoke side as service `hermes-hub`,
account `spoke:Olive:credential` (or env `HERMES_HUB_SPOKE_CREDENTIAL`).
Placing both by hand is deliberate — see VISION.md open question 6; do not
build tooling to automate it, V5b largely retires the problem.

Credentials are treated as opaque strings: never parsed, never logged, never
written to the discovery cache, and never returned in tool output.

### Local files the plugin writes

| Path | Written by | Contents |
|---|---|---|
| `~/.hermes-hub/discovered-peers.json` | `peer_discover` | Spoke skill metadata only — never credentials |

## Local development

Run the tests from the hermes-hub repo, using the repo's own venv (never the
Hermes runtime venv):

```bash
cd /Users/mtwomey/Git_Repos/hermes-hub
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/w3_gate2_isolated_load.py   # isolated-runtime load proof
```
