---
name: hermes-hub-peer
description: >
  Reach other Hermes machines ("peers"/"spokes") from inside a conversation
  via the peer_* tools, routed through a hermes-hub instance.
---

# hermes-hub-peer

Other machines running Hermes connect **outbound** to a hub. From this
session you reach any of them with the `peer_*` tools. A peer runs a full
Hermes agent turn with its own local filesystem, shell, and network access —
so this is how you get at something only that machine can see.

## Discovery is on demand, always

Which peers are online, and what they can do, is **not** in this document and
is **not** in the system prompt. It changes as machines connect and
disconnect, and baking it into the prompt would invalidate conversation
caching for every open session. So: when the user names another machine, or
when a request might be better served elsewhere, **call `peer_list` first**.
It returns who is reachable right now, with each skill's description and
examples.

## Common patterns

| Task | Tool | Notes |
|---|---|---|
| "Which of my machines are up?" | `peer_list` | Start here; nothing else knows |
| "What can Olive do?" | `peer_info` | `peer_name` required |
| Cache a peer's skills locally | `peer_discover` | Same data, persisted |
| "Ask Olive whether she can reach X" | `peer_ask` | Synchronous; returns her answer |
| Continue the same peer conversation | `peer_ask` + `context_id` | Reuse the id from the previous reply |
| Send a local file to a peer | `peer_ask` + `file_path` | Arrives on the peer's disk before its turn |
| Re-read an earlier task's outcome | `peer_status` | `task_id` from a `peer_ask` result |
| Retrieve a file a peer produced | `peer_fetch_artifact` | Needs **both** `task_id` and `artifact_id` |

## Pitfalls

- **`peer_fetch_artifact` needs `task_id` as well as `artifact_id`.** The
  hub's download route is task-scoped; artifact id alone is a 404.
- **A peer that is not connected fails fast.** Requests are not queued for an
  offline machine. If `peer_ask` says a spoke is unavailable, check
  `peer_list` rather than retrying.
- **Credentials are resolved for you.** Each peer checks its own per-peer
  secret; it comes from the environment or Keychain. Do not ask the user to
  paste one into a tool call, and never echo one back.
- **Peers have real side-effect authority.** A peer may write files and run
  commands. Ask for what you actually want done, and be as specific as you
  would be with your own tools.
- **This is synchronous.** A long peer task blocks the tool call.
  `peer_status` reads an existing task; it does not run anything in the
  background.
