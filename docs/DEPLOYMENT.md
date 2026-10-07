# Deploying Hermes Hub services

This guide installs launchd user services on macOS. It supports a hub-only host, a spoke-only host, or both on one host. It never restarts `ai.hermes.gateway` and never installs packages into Hermes's runtime virtual environment.

## Prerequisites

On each host:

1. Clone this repository and create its project venv:

   ```bash
   git clone git@github.com:mtwomey/hermes-hub.git ~/Git_Repos/hermes-hub
   cd ~/Git_Repos/hermes-hub
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   ```

2. Hermes must already be installed (package-managed layout: `~/.hermes/installs/<id>/facts.json` exists). A spoke runs on whichever dependency generation Hermes currently has selected; its wrapper resolves that at start and fails loudly if the transport packages are missing. Nothing is installed into Hermes's runtime. Spoke installs also get the `ai.hermes.post-update` watcher, which restarts the spoke onto the new generation after each Hermes update. See [`POST-UPDATE.md`](POST-UPDATE.md).

3. Provision the required Keychain credentials out of band. Never put tokens or per-spoke credentials in the configuration file, plist, repository, shell history, or logs.

## Local service configuration

Create `~/.config/hermes-hub/service.env` with mode `600`:

```bash
mkdir -p ~/.config/hermes-hub
cp services/hub-service.env.example ~/.config/hermes-hub/service.env
chmod 600 ~/.config/hermes-hub/service.env
```

The installer reads this file for `install`, `reinstall`, and `status`. Environment variables can override one setting for a single invocation.

## Hub-only host

Use a host that accepts inbound spoke connections. Set:

```dotenv
SERVICE_MODE=hub
HUB_BIND_HOST=0.0.0.0
HUB_PORT=8770
HUB_PUBLIC_URL=https://hub.example.invalid:8770
HUB_TASK_TTL_SECONDS=1800
```

`HUB_PUBLIC_URL` must be the reachable address advertised to spokes. A wildcard bind without it is rejected.

Install and verify:

```bash
services/install-hub-services.sh install
services/install-hub-services.sh status
lsof -nP -iTCP:8770 -sTCP:LISTEN
```

## Spoke-only host

A spoke opens an outbound connection to the hub. It does not listen on a port and needs no firewall exception for inbound hub traffic. Set:

```dotenv
SERVICE_MODE=spoke
SPOKE_HUB_HOST=hub.example.invalid
HUB_PORT=8770
SPOKE_NAME=MySpoke
```

Install and verify:

```bash
services/install-hub-services.sh install
services/install-hub-services.sh status
launchctl print gui/$(id -u)/ai.hermes.spoke
launchctl print gui/$(id -u)/ai.hermes.post-update
```

The hub operator must provision the matching spoke credential in the spoke host's Keychain before starting it.

## Combined hub and local spoke

Set both independent endpoint values:

```dotenv
SERVICE_MODE=both
HUB_BIND_HOST=0.0.0.0
HUB_PUBLIC_URL=https://hub.example.invalid:8770
SPOKE_HUB_HOST=127.0.0.1
HUB_PORT=8770
SPOKE_NAME=HubHost
HUB_TASK_TTL_SECONDS=1800
```

Install both services:

```bash
services/install-hub-services.sh install
services/install-hub-services.sh status
```

## Operations and removal

```bash
# Re-render the selected service mode using the local configuration.
services/install-hub-services.sh reinstall

# Inspect selected service registration and listener state.
services/install-hub-services.sh status

# Remove selected services and their plists. Keychain credentials are retained.
services/install-hub-services.sh uninstall

# Add (or reload) only the post-update watcher on an existing spoke host;
# never regenerates or restarts the hub or spoke. Remove it with uninstall-watcher.
services/install-hub-services.sh install-watcher
```

For a mode change, uninstall the old mode first, update `SERVICE_MODE`, then install the new mode. Inspect `~/.hermes/logs/ai.hermes.hub.error.log` or `~/.hermes/logs/ai.hermes.spoke.error.log` if launchd does not keep a service running.

## After a Hermes update

The spoke is moved onto Hermes's new dependency generation automatically by `ai.hermes.post-update` (manual equivalent: `hermes-post-update`). See [`POST-UPDATE.md`](POST-UPDATE.md).
