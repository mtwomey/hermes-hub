# Calling the hub from a non-Hermes agent

Any program on the hub's Mac that can run a shell command or make HTTP
requests can ask a spoke (e.g. Olive) a question. The hub's agent card is
self-describing: once a caller has the hub token, the card tells it
everything else.

## Bootstrap text to give another agent

Paste this into the other agent's instructions:

> To talk to my Hermes peer hub: get the hub token with
> `security find-generic-password -s hermes-hub -a hub:external:token -w`
> (never print, log or store it). Then GET
> `http://127.0.0.1:8770/.well-known/agent-card.json` with header
> `Authorization: Bearer <token>` and follow the instructions in the card,
> especially the required extension `urn:hermes-hub:ext:spoke-routing:v1`.
> Check `connectedSpokes` first; if the spoke you want isn't listed, say so
> instead of retrying.

## What the card provides

| Card field | Contents |
|---|---|
| `description` | Prose: method, URL, the `targetSpoke` / `spokeCredential` metadata keys, where the credentials live |
| `capabilities.extensions[uri=urn:hermes-hub:ext:spoke-routing:v1]` (required) | Structured contract: `connectedSpokes`, `rpcUrl`, `messageMetadata`, `credentials` (Keychain locations and commands, env fallbacks), `methods`, `artifacts`, `files`, `latency`, `exampleRequest` |
| ↳ `requiredHeaders` | `Authorization: Bearer <hub token>`, `A2A-Version: 1.0`, `Content-Type: application/json`. Without `A2A-Version` the hub returns JSON-RPC error -32009 |
| ↳ `localRpcUrl` / `urlNote` | `rpcUrl` is the advertised LAN address; on this Mac the loopback `localRpcUrl` hits the same endpoint |
| ↳ `errors` | HTTP 401 vs JSON-RPC `error` vs `TASK_STATE_FAILED` |
| ↳ `submit` / `poll` | Recommended long-task flow: `SendMessage` with `params.configuration = {"returnImmediately": true}` returns at once with `result.task` (SUBMITTED/WORKING); then `GetTask {"id": <taskId>}` every `intervalSeconds` until one of `terminalStates`. `result.metadata.startedAt` / `lastHeartbeatAt` show liveness |
| ↳ `taskLifetime` | `ttlSeconds` (hub `HERMES_HUB_TASK_TTL_SECONDS`, default 1800; old `HERMES_HUB_TASK_TIMEOUT_SECONDS` is a deprecated alias). A task with no result by then ends FAILED with `hermesError=ttl_expired`. A caller disconnecting or giving up never fails a task; results stay in hub memory until the hub restarts |
| ↳ `hermesErrors` | `status.message.metadata.hermesError` codes: `missing_target_spoke`, `spoke_unavailable`, `spoke_task_failed`, `spoke_disconnected`, `ttl_expired` (the pre-2026-10 `timeout` code no longer exists) |
| ↳ `deduplication` | `messageId`: a `SendMessage` repeating a `message.messageId` the hub has already seen (kept for at least the task TTL) attaches to the existing task and returns it; the spoke is not asked again. Use a fresh `messageId` per logical request and reuse it only when retrying the HTTP call. `hermesPeerTools`: Hermes `peer_ask` returns `state=already_in_progress` (with the running `task_id`, nothing sent) while an earlier ask from the same session to the same spoke is still running; `new_request=true` overrides; the session's last `contextId` with that spoke is reused by default. `spokeMemory`: each spoke shows its agent the caller's last 2 h of requests (max 5; stored 24 h on the spoke only) so a rephrased duplicate is answered by referring to the earlier result. Optional `message.metadata.callerName` labels the caller |
| `securitySchemes.bearerAuth` | Where the hub token is kept |
| `skills[].description` | Which spoke owns the skill and how to address it |

The card never contains a credential **value**. That's enforced by
`tests/test_card_conformance.py::test_card_never_contains_secret_values`.
`tests/test_card_conformance.py` also drives the hub using only fields parsed
from the card, so the card can't silently drift from how routing actually
works.

## Credentials on this Mac

| Secret | Keychain (service `hermes-hub`) | Env fallback |
|---|---|---|
| Hub token (every HTTP request, including the card) | account `hub:external:token` | `HERMES_HUB_TOKEN` |
| Per-spoke caller credential | account `caller:<Spoke>:credential` | `HERMES_HUB_PEER_CREDENTIAL_<SPOKE>` |

## Worked example (curl)

The secrets are read from the Keychain inline and never echoed:

```bash
TOKEN="$(security find-generic-password -s hermes-hub -a hub:external:token -w)"
CRED="$(security find-generic-password -s hermes-hub -a 'caller:Olive:credential' -w)"

# Who's connected?
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8770/.well-known/agent-card.json \
  | python3 -c 'import json,sys;c=json.load(sys.stdin);print([e["params"]["connectedSpokes"] for e in c["capabilities"]["extensions"] if e["required"]][0])'

# Submit without waiting (recommended); prints the task id. Then poll GetTask.
TASK_ID="$(curl -s --max-time 30 -H "Authorization: Bearer $TOKEN" -H 'A2A-Version: 1.0' \
  -H 'Content-Type: application/json' http://127.0.0.1:8770/a2a/v1 \
  -d "$(python3 -c 'import json,sys,uuid;print(json.dumps({"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"configuration":{"returnImmediately":True},"message":{"role":"ROLE_USER","messageId":str(uuid.uuid4()),"parts":[{"text":sys.argv[1]}],"metadata":{"targetSpoke":"Olive","spokeCredential":sys.argv[2]}}}}))' 'What is your hostname?' "$CRED")" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["task"]["id"])')"
curl -s --max-time 30 -H "Authorization: Bearer $TOKEN" -H 'A2A-Version: 1.0' \
  -H 'Content-Type: application/json' http://127.0.0.1:8770/a2a/v1 \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"GetTask\",\"params\":{\"id\":\"$TASK_ID\"}}"
# Repeat GetTask until status.state is terminal. Do NOT resend the SendMessage
# with a new messageId to "retry" a slow task: it would start a second task.
# (Resending the SAME messageId is safe: the hub returns the existing task.)

# Ask (blocking form, waits up to the task TTL; streaming form is SendStreamingMessage over SSE)
curl -s --max-time 1830 -H "Authorization: Bearer $TOKEN" -H 'A2A-Version: 1.0' \
  -H 'Content-Type: application/json' http://127.0.0.1:8770/a2a/v1 \
  -d "$(python3 -c 'import json,sys,uuid;print(json.dumps({"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"role":"ROLE_USER","messageId":str(uuid.uuid4()),"parts":[{"text":sys.argv[1]}],"metadata":{"targetSpoke":"Olive","spokeCredential":sys.argv[2]}}}}))' 'What is your hostname?' "$CRED")"
```

Passing `$CRED` as an argument briefly exposes it in the process list on
this Mac. That's fine for interactive use. For automation, read it from the
Keychain inside the program instead.

## Before connecting another agent

Anything holding the hub token plus a spoke's credential can make that
spoke run a full agent turn on its machine. For Olive, that's the Robert
Half work laptop. Only give the bootstrap text to agents you'd trust with
that.
