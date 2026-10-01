# Self-Describing Agent Card Implementation Plan

> **For Hermes:** Implement task-by-task with TDD. Every checkpoint gate must be
> reproduced with real command output before moving on; do not accept a
> sub-agent's self-report for any gate.

**Goal:** A non-Hermes agent on this Mac, given only (a) where the hub token
lives in the Keychain and (b) the card URL, can read the hub's agent card and
from it alone successfully ask a named spoke (e.g. Olive) a question.

**Architecture:** The card stays token-gated exactly as today (decision 1 —
no public card). Everything a caller needs beyond the token is moved *into*
the card: a corrected description, a required A2A `AgentExtension` declaring
the hub's custom routing/credential metadata, a filled-in bearer-scheme
description, and per-skill addressing text. A conformance test drives the hub
using only fields parsed from the card JSON so card and routing cannot drift.

**Tech Stack:** Python 3.11, `a2a-sdk` (protobuf `AgentCard`,
`AgentExtension`, `google.protobuf.Struct`), Starlette, pytest, repo `.venv`.

**Scope (decision 2):** this Mac only. Credential locations in the card are
documented as *this machine's convention* (macOS Keychain service
`hermes-hub`). No cross-machine portability work. No MCP adapter (W6) in this
plan.

---

## 0. Current state (verified 2026-10-01)

| Fact | Evidence |
|---|---|
| Card is token-gated; unauthenticated GET → 401 | `curl http://127.0.0.1:8770/.well-known/agent-card.json` → `{"error":"unauthorized",...}`; `hub_server.py:58-87` |
| Card description tells callers to address skills by `"<spoke>::<skill-id>"` — **wrong** | `agent_card.py:84-85` |
| Real routing key is `message.metadata.targetSpoke`; missing → task FAILED "No targetSpoke specified" | `hub_executor.py:98-111` |
| Per-spoke caller credential travels as `message.metadata.spokeCredential`; hub relays opaquely | `hub_executor.py:99-104`, `hub_client.py:127-129` |
| Hub external token in Keychain `hermes-hub` / `hub:external:token` (present on this Mac) | `credentials.py:57`; `security find-generic-password -s hermes-hub -a hub:external:token` → found |
| Caller credential for Olive in Keychain `hermes-hub` / `caller:Olive:credential` (present) | `peer_tools.py:155`; Keychain probe → found |
| Plugin token resolution: arg → `HERMES_HUB_TOKEN` → `~/.hermes-hub/config.json` → **Keychain** `hub:external:token` | `peer_tools.py:99-109`. (Correction to earlier chat claim that the plugin did not read the Keychain — it does, as the last fallback; config.json `hub_token` is empty here, so the Keychain is what's used.) |
| `AgentCapabilities.extensions` / `AgentExtension{uri,description,required,params}` available | `.venv` introspection of `a2a.types` |
| JSON-RPC methods served: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, … | `a2a/server/routes/jsonrpc_dispatcher.py:114-120` |
| Hub runs as launchd `ai.hermes.hub`, from `scripts/run_hub.py` | `~/Library/LaunchAgents/ai.hermes.hub.plist`, `services/hermes-hub-wrapper.sh` |

## 1. Design decisions

1. **Single token-gated card.** No public/extended split. Caller bootstrap is
   out of band: "token is in Keychain `hermes-hub`/`hub:external:token`; GET
   the card with it; follow the card."
2. **Card never contains a secret value.** It contains only Keychain
   *locations* and env-var *names*. Enforced by a test that injects known
   token/credential values and asserts none appear in the serialized card.
3. **Machine-readable + human/LLM-readable.** The routing contract lives in a
   required `AgentExtension` (`uri = "urn:hermes-hub:ext:spoke-routing:v1"`)
   whose `params` Struct is the structured source of truth; the card
   `description` and extension `description` are prose renderings of the same
   constants. One module-level constants block feeds all three, so the prose
   can't contradict the params.
4. **No `documentation_url`.** The repo is private and the card is already
   complete; a URL the caller can't fetch is worse than none. `docs/CALLERS.md`
   is for humans and contains the bootstrap text to paste into another agent.
5. **Skill ids keep the `<spoke>::<id>` namespace** (still useful for
   uniqueness) but each skill description says explicitly *how* to address it
   (`metadata.targetSpoke = "<spoke>"`).
6. **Recommended call is `SendStreamingMessage`.** Task 3 verifies empirically
   what non-streaming `SendMessage` returns for a slow spoke; the card states
   the verified behavior, not an assumption.

## 2. Target card content (shape)

```jsonc
{
  "name": "hermes-hub",
  "description": "hermes-hub relays requests to named Hermes agents (\"spokes\") ... Connected now: Olive, Pumpkin.\nHOW TO ASK A SPOKE: POST JSON-RPC 2.0 to <rpc url>, method SendStreamingMessage, Authorization: Bearer <hub token>. Put the target spoke's name in params.message.metadata.targetSpoke and that spoke's caller credential in params.message.metadata.spokeCredential. Routing is by targetSpoke only — skill ids are informational. ... See capabilities.extensions[urn:hermes-hub:ext:spoke-routing:v1] for the full contract.",
  "supportedInterfaces": [{"url": "http://127.0.0.1:8770/a2a/v1", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
  "capabilities": {
    "streaming": true,
    "extensions": [{
      "uri": "urn:hermes-hub:ext:spoke-routing:v1",
      "required": true,
      "description": "<prose rendering of params>",
      "params": {
        "connectedSpokes": ["Olive", "Pumpkin"],
        "messageMetadata": {
          "targetSpoke":     {"required": true,  "meaning": "exact spoke name; the only routing key"},
          "spokeCredential": {"required": false, "meaning": "opaque per-spoke caller secret; the spoke rejects the task if it does not match"}
        },
        "credentials": {
          "hubToken":        {"use": "Authorization: Bearer", "keychainService": "hermes-hub", "keychainAccount": "hub:external:token",
                              "command": "security find-generic-password -s hermes-hub -a hub:external:token -w", "envFallback": "HERMES_HUB_TOKEN"},
          "spokeCredential": {"keychainService": "hermes-hub", "keychainAccountTemplate": "caller:{spoke}:credential",
                              "command": "security find-generic-password -s hermes-hub -a 'caller:{spoke}:credential' -w",
                              "envFallbackTemplate": "HERMES_HUB_PEER_CREDENTIAL_{SPOKE_UPPER}"},
          "scope": "These are the conventions on the hub's own Mac. Never echo or log the values."
        },
        "methods": {
          "ask":      "SendStreamingMessage (SSE; each line 'data: {json}')",
          "result":   "on result.task|result.statusUpdate with status.state == TASK_STATE_COMPLETED, answer = join(status.message.parts[].text); TASK_STATE_FAILED = error text",
          "followUp": "reuse the returned contextId in params.message.contextId to continue a conversation",
          "lookup":   "GetTask {\"id\": <task id>}"
        },
        "artifacts":    "GET <hub>/a2a/artifacts/{taskId}/{artifactId} with the same bearer token",
        "latency":      "a spoke runs a full agent turn; expect 30s–5min; hub task timeout <N>s",
        "exampleRequest": { "jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage",
                            "params": {"message": {"role": "ROLE_USER", "messageId": "<uuid>",
                                       "parts": [{"text": "..."}],
                                       "metadata": {"targetSpoke": "Olive", "spokeCredential": "<from keychain>"}}}}
      }
    }]
  },
  "securitySchemes": {"bearerAuth": {"httpAuthSecurityScheme": {"scheme": "Bearer",
      "description": "Hub token. On this Mac: security find-generic-password -s hermes-hub -a hub:external:token -w (env fallback HERMES_HUB_TOKEN)."}}},
  "skills": [{"id": "Olive::general-reasoning", "description": "[spoke: Olive — address with metadata.targetSpoke=\"Olive\"] ...", "tags": ["spoke:Olive"]}]
}
```

Exact wording is the implementer's; field *presence and values* are pinned by
the tests in Tasks 1–5.

## 3. Tasks

All commands run from `/Users/mtwomey/Git_Repos/hermes-hub` with
`.venv/bin/python`. Baseline first:

**Gate 0 (baseline).** `.venv/bin/python -m pytest tests/ -q` → record pass
count; must be all-pass before starting. `git status` clean on a new branch
`mtwomey/self-describing-card`.

### Task 1: Routing-contract constants

**Files:** Create `hermes_hub/caller_contract.py`; Test `tests/test_caller_contract.py`.

Single source of truth used by `hub_executor.py`, `hub_client.py`,
`peer_tools.py`, and `agent_card.py`:

```python
EXTENSION_URI = "urn:hermes-hub:ext:spoke-routing:v1"
META_TARGET_SPOKE = "targetSpoke"
META_SPOKE_CREDENTIAL = "spokeCredential"
KEYCHAIN_SERVICE = "hermes-hub"
HUB_TOKEN_ACCOUNT = "hub:external:token"
CALLER_CREDENTIAL_ACCOUNT_TEMPLATE = "caller:{spoke}:credential"
ENV_HUB_TOKEN = "HERMES_HUB_TOKEN"
ENV_CALLER_CREDENTIAL_TEMPLATE = "HERMES_HUB_PEER_CREDENTIAL_{SPOKE_UPPER}"
RECOMMENDED_METHOD = "SendStreamingMessage"
```

Steps: write test asserting `hub_executor` / `hub_client` / `peer_tools`
import these names (grep-style test: no remaining string literal
`"targetSpoke"` or `"caller:"` outside `caller_contract.py`) → RED → refactor
the three modules to import constants → GREEN → full suite still green →
commit `refactor: centralise caller routing contract constants`.

### Task 2: Card content

**Files:** Modify `hermes_hub/agent_card.py:66-115`; Test `tests/test_agent_card.py`.

New tests (write first, watch fail):

- `test_card_declares_required_spoke_routing_extension` — exactly one
  extension with `EXTENSION_URI`, `required is True`, params include
  `messageMetadata.targetSpoke.required == True`, `connectedSpokes` equals
  the registry's names.
- `test_card_documents_keychain_locations_not_values` — params
  `credentials.hubToken.keychainAccount == "hub:external:token"`,
  `credentials.spokeCredential.keychainAccountTemplate ==
  "caller:{spoke}:credential"`.
- `test_card_description_states_targetSpoke_routing_and_drops_namespaced_hint`
  — description contains `targetSpoke` and `spokeCredential`; does **not**
  contain `Address a specific spoke's skill by its namespaced id`.
- `test_bearer_scheme_description_names_keychain_account`.
- `test_skill_description_tells_caller_how_to_address_spoke` — contains
  `targetSpoke="Olive"`.
- `test_card_example_request_is_valid_SendMessageRequest` — parse
  `params.exampleRequest.params` into the SDK's `SendMessageRequest` proto
  (via `google.protobuf.json_format.ParseDict`) without error.
- Update existing `test_card_reflects_registry_changes` so `connectedSpokes`
  also tracks registry changes.

Implementation: build `params` as a dict → `google.protobuf.struct_pb2.Struct`
(`Struct().update(d)`), append `AgentExtension` to
`card.capabilities.extensions`; render prose from the same dict. Add a
`task_timeout_seconds` arg to `build_hub_agent_card` and pass it from
`hub_server.build_hub_app` so the card's latency line is the real value.

Commit `feat(card): self-describing spoke routing contract`.

### Task 3: Verify non-streaming SendMessage behavior (empirical, no guessing)

**Files:** Test `tests/test_hub_server.py` (or `tests/test_card_conformance.py`).

Using `tests/hub_harness.py` with a fake spoke that delays ~2 s then replies,
POST `SendMessage` (non-streaming). Record what comes back: completed task
with answer, or a non-terminal task requiring `GetTask` polling. Pin the
observed behavior in a test, then make the card's `methods` text say exactly
that (e.g. "SendMessage blocks until terminal" **or** "SendMessage returns a
WORKING task; poll GetTask"). Capture the raw response in the commit message
body.

**Gate 1:** `.venv/bin/python -m pytest tests/test_agent_card.py
tests/test_caller_contract.py -q` all pass; paste the serialized card from a
2-spoke registry (`agent_card_json(...)` dumped via a one-liner) and eyeball:
no `<spoke>::<skill-id>` addressing hint, extension present, no secret values.

### Task 4: No-secret-leak test

**Files:** Test `tests/test_agent_card.py`.

`test_card_never_contains_secret_values`: build app with
`expected_external_token="TOK-sentinel-123"`, register a spoke, monkeypatch
the Keychain reader / env (`HERMES_HUB_TOKEN`,
`HERMES_HUB_PEER_CREDENTIAL_OLIVE`) to sentinel values, fetch card through
`TestClient` with the token, assert none of the sentinels appear anywhere in
`resp.text`. Commit `test(card): card never carries credential values`.

### Task 5: Card-only conformance test (the drift guard)

**Files:** Create `tests/test_card_conformance.py`.

A "naive caller" that knows **only** the hub base URL and the token:

1. GET `/.well-known/agent-card.json` with bearer token.
2. From the JSON alone, read: RPC URL (`supportedInterfaces[0].url`), the
   required extension by `EXTENSION_URI`, the metadata key names from
   `params.messageMetadata`, the method from `params.methods`/
   `exampleRequest.method`.
3. Build the request by filling `exampleRequest` (replace `targetSpoke`,
   text, credential) — **no imports from `hermes_hub`** other than the
   harness used to start the server and attach a fake spoke with
   `expected_credential`.
4. Stream SSE; assert the fake spoke's answer arrives with
   `TASK_STATE_COMPLETED`.
5. Negative: same request without `spokeCredential` → `TASK_STATE_FAILED`
   (proves the card's "spoke rejects mismatch" statement is true).

Add a lint-style assertion that the test file does not `import` from
`hermes_hub` except `tests.hub_harness`. Commit
`test(card): conformance — card alone suffices to route a task`.

**Gate 2:** full suite `.venv/bin/python -m pytest tests/ -q` → baseline
count + new tests, 0 failures. `scripts/w3_gate2_isolated_load.py` still
passes (plugin unaffected).

### Task 6: Caller documentation

**Files:** Create `docs/CALLERS.md`; Modify `README.md` (one link line),
`plugin/hermes_hub_peer/README.md:58-72` (note Keychain fallback for hub
token, correcting the table which omits it).

`docs/CALLERS.md` contains:

- What a non-Hermes caller needs and the security note (any caller holding
  the token + a spoke credential can make that spoke run a full agent turn;
  for Olive that is the Robert Half laptop).
- **The bootstrap block to paste into another agent**, verbatim:

  > To talk to my Hermes peer hub: get the hub token with
  > `security find-generic-password -s hermes-hub -a hub:external:token -w`
  > (do not print or store it). Then GET
  > `http://127.0.0.1:8770/.well-known/agent-card.json` with header
  > `Authorization: Bearer <token>` and follow the instructions in the card,
  > especially the required extension `urn:hermes-hub:ext:spoke-routing:v1`.

- A worked `curl` example (token and credential read inline from Keychain
  via `$(security …)`, never echoed).

Commit `docs: caller guide for non-Hermes agents`.

### Task 7: Deploy and live verification

1. Merge branch to `main` (after Matthew's review of the diff).
2. Restart hub: `launchctl kickstart -k gui/$(id -u)/ai.hermes.hub`.
   Spokes reconnect automatically; confirm with
   `curl -s -H "Authorization: Bearer $(security find-generic-password -s hermes-hub -a hub:external:token -w)" http://127.0.0.1:8770/health`
   → `connected_spokes` includes `Pumpkin` (and `Olive` if online).
   Do **not** restart `ai.hermes.gateway`.

**Gate 3 (live card):** same curl against `/.well-known/agent-card.json`;
capture JSON; verify extension present, `connectedSpokes` matches `/health`,
and grep the output for the actual token/credential values → 0 matches
(compare in-process; never print the secrets).

**Gate 4 (cold-agent acceptance — the real goal).** Spawn a fresh subagent
with **no** hermes-hub repo access in its context and only the bootstrap
block from `docs/CALLERS.md`, plus terminal access. Task: "Ask Pumpkin what
the current hostname is, and report the answer." PASS = it reaches
`TASK_STATE_COMPLETED` with a correct hostname, verified independently by
Hermes with `hostname`. Repeat targeting Olive only if `/health` shows Olive
connected; otherwise record "Olive offline — not exercised". Review the
subagent transcript: it must not have printed the token or credential.

**Gate 5 (regression).** In a normal Hermes session, `peer_list` and
`peer_ask Pumpkin` still work.

## 4. Risks / open points

- **Token is sole gate to a full agent turn on Olive.** Unchanged by this
  plan, but this plan makes it easier to use. Out-of-scope mitigation
  (per-caller tokens) noted for later.
- **Struct serialization:** `google.protobuf.Struct` turns ints into floats
  (`300` → `300.0`). Tests should compare numerically; prose should format
  ints explicitly.
- **SDK version drift:** if `a2a-sdk` changes extension serialization keys,
  Task 5 conformance test fails loudly — intended.
- **Another vendor's agent may not be able to reach `127.0.0.1`** (cloud-run
  tools). That is a property of the client app, not the hub; Gate 4 proves
  the card is sufficient, not that any particular app can use it.
- **Hub restart** briefly drops in-flight peer tasks; do it when no peer
  task is running.
