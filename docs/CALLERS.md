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

# Ask (blocking form; the streaming form is SendStreamingMessage over SSE)
curl -s --max-time 330 -H "Authorization: Bearer $TOKEN" -H 'A2A-Version: 1.0' \
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
