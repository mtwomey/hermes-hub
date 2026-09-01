# Hermes Peer Agents — Vision & Architecture

**Status:** Living document. This is the parent doc that the tactical
milestone plans in `.hermes/plans/` serve. When a tactical plan and this
document disagree, this document is the intent and the plan is wrong.

**Written:** 2026-09-01, from a design conversation with Matthew, after
hermes-peer M0–M6 (mesh A2A) and hermes-hub M0–M6 (hub-and-spoke) were both
built. It exists because neither of those had a parent: both were tactical
transport plans with no shared statement of what the feature is *for*.

---

## 1. The vision

> I want to be working in Hermes in a session in the Hermes desktop app and
> think to myself, "Hey, you know what? I bet my Hermes running on a
> different computer can check this for me, or has access to this thing." So
> then I ask my current Hermes, "please check with Olive and see if she can
> access this." Or "get this file from Pumpkin and use it to update your own
> script."
>
> — Matthew, 2026-09-01

Everything below serves that paragraph. The test of any design decision is
whether it makes that interaction more natural, not whether it makes the
protocol more correct.

### What this is NOT

- Not a CLI feature. `hermes a2a ask Olive "..."` is scaffolding for testing,
  not the product. If the only way to use this is to leave the conversation
  and type a command, the vision has failed.
- Not a multi-tenant agent platform. Both endpoints are Matthew's own
  machines. Trust boundaries are drawn accordingly (see V4).
- Not a general A2A interop play. Spec compliance is a means (it gets us a
  well-specified wire format and a real SDK), not the goal.

### Concrete "done" scenarios

These are the acceptance bar. Each is a thing said in a desktop session,
with no CLI, no manual process starting, no copy-pasted tokens:

1. **Remote capability check** — "Ask Olive whether she can reach Dremio, and
   if so what tables are in the sales schema."
2. **File transfer into real work** — "Get `normalize.clj` from Pumpkin and
   use it to update your own version of the script." *(This is the first
   real task, per V8 — the one that proves usefulness, not just liveness.)*
3. **Remote second opinion** — "Ask Olive to review this migration plan and
   tell me what I'm missing."
4. **Suggested delegation** — Matthew describes a need; the model notices a
   connected peer is better positioned and offers: "Olive has the Jira
   plugin configured — want me to ask her?"

---

## 2. Decision record

Settled in conversation on 2026-09-01. These supersede conflicting decisions
in the tactical plans (notably hermes-hub H8, which was written correctly but
implemented wrong — see §4).

| # | Topic | Decision | Rationale / consequence |
|---|---|---|---|
| V1 | Primary surface | **In-session model tools**, not a CLI. The model calls peer tools during normal conversation. | The vision is conversational. CLI stays as a test/debug harness only. |
| V2 | Transport | **Hub-and-spoke over outbound-only WebSocket** (hermes-hub's H2/H3). | Proven necessary: Olive is Jamf/CyberArk-managed, inbound is blocked, and no self-service fix exists. Mesh is structurally fragile on managed endpoints. |
| V3 | Capability discovery | **Both explicit naming and model inference**, implemented **cache-safely**: a short static line in the system prompt plus rich `peer_list`/`peer_info` tool descriptions. Peer capabilities are fetched on demand, never injected into the system prompt. | Hermes treats per-conversation prompt caching as sacred. Injecting live peer state would invalidate the cache for every open session on every spoke connect/disconnect. Cost: the model makes a visible discovery call instead of "just knowing." Accepted. |
| V4 | Remote permissions | **Full side effects allowed.** A peer may write files, run commands, and use side-effecting tools. | All machines are Matthew's. Mirrors hermes-peer D8. See V5 for the security consequence this creates and how it is bounded. |
| V5 | Authorization model | **Per-peer keys, enforced at the SPOKE, not the hub.** Three distinct auth questions: (a) may this spoke join? — hub checks, (b) may this caller reach the hub? — hub checks, (c) may this caller command *this specific spoke*? — **the spoke checks, using its own local secret**. The hub relays the caller's credential opaquely: never validates it, never stores it. | Restores the mesh property that compromising one key reaches one machine. Hub-side enforcement would NOT achieve this — a hub holding verification material for every spoke is a single point of total compromise, exactly what this decision prevents. Keeping the hub out of the trust path is also what makes V8 (hub on a cheap always-on box) safe. **hermes-hub as built has a single shared token and NO per-spoke check anywhere — a defect to fix, not a design to keep.** |
| V5a | Credential form (now) | **Shared per-spoke secret** carried in an opaque credential field on the task frame; spoke compares against its own Keychain entry before executing. | Option B of four considered. Buys the "hub is not a fleet-wide key" property at roughly the cost of the weaker hub-side alternative. **Accepted residual risk:** the hub sees credentials in flight and could replay them. Acceptable for two personal machines on a trusted LAN; this is precisely what V5b fixes. |
| V5b | Credential form (planned) | **Request signing / keypair auth.** The spoke verifies a signature instead of comparing a secret; the hub can no longer forge or usefully replay. Also retires V5a's shared-secret distribution problem: no secret to copy between machines, rotation is local, and trust grows linearly (publish a public key) instead of pairwise. | Already on hermes-peer's original roadmap ("keypair auth / request signing"). **Design constraint on V5a: the credential field MUST be opaque bytes with no assumed structure**, so moving to signatures changes only what the caller puts in and what the spoke checks — no change to the hub, the frames, or routing. If V5a is built in a way that makes V5b expensive, V5a was built wrong. |
| V6 | Execution mode | **Synchronous by default; asynchronous for long work.** | Sync matches normal tool-call ergonomics. Async requires task persistence and a way to surface results back into a session later — real work, previously deferred as hermes-peer M8 and never built. Design is open (see §6). |
| V7 | Direction | **Bidirectional and symmetric.** Either machine can ask the other. | Consequence: with the hub on Pumpkin, Olive can reach nothing while Pumpkin sleeps. Accepted for now; the main argument for always-on hub hardware later. |
| V8 | Hub placement | **Pumpkin now; portable to always-on hardware (Raspberry Pi / Linux box) later.** Hub code stays pure-Python with no macOS-only dependencies. | Keeps the move cheap when it happens. Hub must not grow a dependency on Hermes core internals. |
| V9 | First proof of usefulness | **File transfer into real work** — scenario 2 above. | Chosen over remote-tool and remote-reasoning scenarios. Drives artifact support to the front of the queue (see §4 — it is currently the biggest gap). |
| V10 | Local Hermes is a spoke too | **Pumpkin's own Hermes auto-connects to the hub as a spoke**, alongside Olive. | Uniform model: the desktop session reaches every machine the same way, including its own. No special-casing "local." Also means no manual process starting (see V1). |
| V11 | Client agnosticism | **The hub is not Hermes-specific.** Any A2A-compliant client is a first-class caller — Claude Desktop, `curl`, future tools. Hermes is the *primary* client, never a *required* one. | Corrects a design error caught 2026-09-01: an injection-based async story would have made "must be a Hermes agent" a structural requirement of the hub, contradicting V8's portability intent. |
| V12 | Async delivery mechanism | **Durable mailbox + client-side polling**, using A2A's own `GetTask`. The hub stores task state and results; clients retrieve on their own schedule. **No push, no injection, no webhooks in the hub.** | The only client-agnostic option, and it is spec-native. Rejected: gateway message injection (Hermes-only, violates V11), webhooks (needs an inbound listener on the client — the exact thing V2 exists to avoid; also hermes-peer's D14). Hermes-specific conveniences (e.g. auto-surfacing a result via `inject_message`) may exist as an optional *client-side* layer, never in the hub. |
| V13 | Non-Hermes access path | **An MCP server fronting the hub** is the intended integration for MCP-native clients like Claude Desktop. **Rule: the adapter is always deployed local to its client, holding only that machine's credentials — never as a shared remote service.** | Claude Desktop speaks MCP natively; asking it to speak raw A2A is friction with no benefit. The MCP server is a thin adapter over the hub's existing A2A surface — an additional front door, not a second protocol in the core. The locality rule matters: a *shared* adapter holding every client's per-spoke keys would become exactly the fleet-wide concentration point V5 exists to prevent. Local to its client, it is merely a credential holder — structurally the same as Hermes reading the Keychain. |

---

## 3. Target architecture

```
  CALLERS (any A2A client — V11)
  ┌─────────────────────┐  ┌──────────────────┐  ┌──────────┐
  │ Hermes desktop      │  │ Claude Desktop   │  │ curl /   │
  │ session (peer_*     │  │ (via MCP adapter │  │ future   │
  │ model tools)        │  │  — V13)          │  │ clients  │
  └──────────┬──────────┘  └────────┬─────────┘  └────┬─────┘
             │                      │                 │
             └──────────────┬───────┴─────────────────┘
                            ▼
   ┌────────────────────────────────────┐        outbound WS    ┌────────────────┐
   │              HUB                   │◄──────────────────────│ Spoke: Olive   │
   │      (Pumpkin now, RPi later)      │◄─────────────┐        │ (managed Mac,  │
   │                                    │              │        │  inbound       │
   │ • the ONLY listener                │              │        │  BLOCKED — ok) │
   │ • spoke registry                   │              │        └────────────────┘
   │ • per-peer authorization (V5)      │              │
   │ • durable task mailbox (V12)       │       outbound WS     ┌────────────────┐
   │ • A2A externally; client-agnostic  │◄─────────────────────►│ Spoke: Pumpkin │
   │ • NO push / NO injection           │                       │ (local Hermes) │
   └────────────────────────────────────┘                       └────────────────┘
```

Properties that matter:

- **No spoke ever binds a listening socket.** This is the whole reason the
  hub exists. Any change that reintroduces an inbound requirement on a spoke
  is a regression against V2.
- **No caller is required to bind a listening socket either.** Async results
  are polled from the mailbox, never pushed (V12). This is what keeps a
  laptop client, a phone, or a sandboxed desktop app viable as a caller.
- **The hub is standalone and client-agnostic.** No dependency on Hermes core
  internals (V8), and nothing in its contract assumes the caller is Hermes
  (V11). It can move hosts, and it can serve tools that do not exist yet.
- **The hub speaks A2A externally.** An outside caller sees a normal,
  spec-compliant A2A agent. Hub-and-spoke is an internal routing detail.
  The MCP server (V13) is an adapter in front of this, not a parallel core.
- **The hub is untrusted for authorization.** It routes; it does not decide
  who may command a spoke (V5). It holds no per-spoke verification material
  and relays caller credentials opaquely. Compromising the hub yields traffic
  and reachability, **not** the ability to command the fleet. This is what
  makes V8's "put it on a Raspberry Pi in a closet" safe.
- **Spokes execute with full local authority** (V4) — and therefore each spoke
  is the enforcement point for whether a given caller may command it (V5).

---

## 4. Honest current state

Two working codebases, neither of which is the product yet.

| Capability | hermes-peer (mesh) | hermes-hub (hub-spoke) | Target |
|---|---|---|---|
| In-session model tools | ✅ 6 tools (`peer_ask`, `peer_list`, `peer_info`, `peer_discover`, `peer_status`, `peer_fetch_artifact`) | ❌ **none** — CLI only | **Required (V1)** |
| Artifacts / file transfer | ✅ inline + authenticated URL, SHA-256, verified to 100KB | ⚠️ **inline text only**; binary/large explicitly "not built in this version" | **Required (V9)** |
| Loads into the live session | ✅ `.pth` + gateway shim | ❌ standalone by design | **Required (V1/V10)** |
| Survives Olive's firewall | ❌ inbound blocked | ✅ solved, proven | **Required (V2)** |
| Per-peer authorization | ✅ per-peer Keychain tokens | ❌ **single shared token, no per-spoke check** | **Required (V5)** |
| Multi-turn continuity | ✅ verified | ✅ verified (independently re-verified 2026-09-01) | Keep |
| Incremental streaming | ✅ verified | ✅ verified (independently re-verified 2026-09-01) | Keep |
| Async / resumable tasks | ❌ planned as M8, never built | ❌ not built | **Required (V6)** |

**The blunt read:** hermes-hub solved the transport problem correctly and
dropped both things that make the feature *useful* — the tool surface and
real artifact handling. hermes-peer has those but can't reach Olive. Neither
alone delivers the vision.

**The synthesis is the work:** hermes-hub's transport + hermes-peer's tool
surface and artifact handling + per-peer keys + local-Hermes-as-spoke.

### Known defects to fix (not new features)

1. **hermes-hub has no per-spoke authorization.** `Router.route_task()` looks
   up a spoke by name and sends, with no permission check. Its own decision
   record (H8) called for per-spoke tokens; the implementation collapsed to a
   single shared `expected_spoke_token`. No gate caught it because no gate
   asked. Violates V5.
2. **hermes-hub artifacts are inline-text-only.** Cannot carry a binary or a
   large file — directly blocks V9, the chosen first real task.

---

## 5. Resolving the three-mechanism problem

Three overlapping peer mechanisms currently exist on these machines. This has
been an open question since the core's own `hermes peer` shipped independently;
it is now decided:

| Mechanism | Disposition |
|---|---|
| **Core Hermes `hermes peer`** (bot-to-bot DMs over `api_server`) | **Leave alone.** Unrelated feature, shipped by core, not ours to retire. Our CLI already renamed to `hermes a2a` to avoid the collision. |
| **hermes-peer** (mesh A2A) | **Reference implementation and fallback.** Not deleted while hermes-hub is incomplete — it is currently the only thing with a working tool surface and real artifact support. Retire only once hermes-hub demonstrably supersedes it on every row of the §4 table. |
| **hermes-hub** (hub-and-spoke) | **The go-forward product.** All new work lands here. |

Retiring hermes-peer is a future decision with a clear trigger: every ❌ and
⚠️ in the hermes-hub column of §4 turned ✅, proven by a real task, not a
test.

---

## 6. Workstreams

Ordered by dependency, not priority. Each becomes its own gated tactical plan
under `.hermes/plans/`; this document only sets scope and intent.

**W1 — Per-peer authorization (defect, V5/V5a).**
Replace the single shared token with spoke-enforced per-peer credentials.
Concretely: add an **opaque** credential field to the task frame (currently
`{type, task_id, context_id, text, metadata}` — there is nowhere for caller
authorization to travel); the caller supplies its per-spoke secret; the hub
relays it without validating or storing it; the spoke checks it against its
own Keychain entry **before executing** (today the spoke validates nothing
about an inbound task and will run anything that arrives on its socket).
Keychain-backed on macOS; portable equivalent for the future Linux/RPi hub.
**The credential field must carry opaque bytes with no assumed structure** so
V5b (signatures) is a later change to the endpoints only, never to the hub or
the wire format.

**W2 — Real artifact transfer (defect, V9).**
Binary-safe, size-tolerant file movement spoke→hub→caller and back, SHA-256
verified. hermes-peer's `artifacts.py` is the working reference — inline under
a threshold, authenticated URL above it.

**W3 — In-session tool surface (V1, V3, V10).**
The heart of the vision. Peer tools registered into the live Hermes runtime;
Pumpkin's own Hermes auto-connects as a spoke; hub runs as a managed service
rather than a hand-started foreground process. Cache-safe discovery per V3 —
static prompt line, rich tool descriptions, on-demand `peer_list`/`peer_info`.

**W4 — First real task (V9).**
Not a test: actually pull a real file from one machine and use it to change
something on the other, in a normal conversation. This is the acceptance gate
for the whole effort. Everything before it is plumbing.

**W5 — Async / long-running tasks (V6, V12).**
Durable task mailbox in the hub: task state and results persist across spoke
disconnect, hub restart, and client absence. Retrieval is client-side polling
via A2A's `GetTask`, plus a "what finished that I haven't collected?" query so
one call surfaces everything pending. **No push, no injection, no webhooks** —
the hub never requires anything of the client but the ability to ask.
Absorbs hermes-peer's never-built M8. Open: how a request becomes async in the
first place (see §7).

**W6 — MCP adapter for non-Hermes clients (V13).**
A thin MCP server fronting the hub's existing A2A surface, so Claude Desktop
and other MCP-native clients are first-class callers. Deliberately an adapter:
it adds a front door, not a second protocol in the core. Worth building once
W1–W4 prove the hub is worth connecting to.

**W7 — Hub as always-on hardware (V8).**
Deferred until W1–W4 prove the value. Linux/RPi deployment, service
definition, credential storage without macOS Keychain.

---

## 7. Open questions

1. **How does a request become async in the first place?** (W5) Model judges
   it will be slow? Matthew says so explicitly ("ask Olive in the background
   to...")? Start sync and auto-convert on timeout? Undecided — affects tool
   schema design, so decide before W5 starts.
2. **When does a client check the mailbox?** (V12, W5) Polling is the
   mechanism, but the *cadence* is a client-side UX decision. For Hermes:
   on demand only, or a nudge at natural boundaries? Note this is genuinely
   the client's problem, not the hub's — which is the point of V12.
3. **What does the model see in `peer_list`?** Skill IDs are machine-ish
   (`Olive::general-reasoning`). For V3's "model suggests a peer" to work well,
   descriptions must be genuinely useful prose, not identifiers.
4. **Spoke identity vs. machine identity.** If Pumpkin runs multiple Hermes
   profiles, is each a distinct spoke? hermes-peer's roadmap listed
   "profile-specific peers"; unresolved.
5. **What happens to a spoke's in-flight task when the hub restarts?** W5's
   mailbox covers *completed* results; an in-flight task at restart is a
   distinct case. Re-dispatch, fail cleanly, or resume?
6. **Credential distribution — deliberately NOT solved for V5a; revisit at V5b.**
   V5a needs the same secret in two Keychains (caller's and spoke's), placed by
   hand. That is acceptable at two machines and is what hermes-peer already
   does. **Do not build tooling to automate this** — V5b largely retires the
   problem rather than automating it: with keypairs there is no shared secret,
   each machine keeps its own private key, and rotation becomes local. What
   survives into V5b is *enrollment* ("Olive, trust this public key, it's
   Pumpkin") — the same shape as SSH `authorized_keys`: still a step, but
   nothing secret is at stake, a leaked public key is a non-event, and it
   grows linearly with the fleet instead of pairwise. Settle the enrollment
   UX when V5b is implemented, not before.

---

## 8. Rejected approaches

Recorded so they are not re-proposed. Each was seriously considered.

| Approach | Why rejected |
|---|---|
| **Mesh topology** (every machine listens) | Structurally fragile on IT-managed endpoints. Proven by real failure: Olive's Jamf/CyberArk-managed firewall blocks inbound with no self-service fix. This is what hermes-peer built and what hermes-hub exists to replace. |
| **Gateway message injection for async results** | Hermes-runtime-specific. Would have made "must be a Hermes agent" a structural requirement of the hub, contradicting V11. Still viable as an *optional client-side* convenience for Hermes callers; never in the hub. |
| **Webhooks / push notifications to callers** | Requires the caller to accept an inbound connection — precisely the constraint V2 exists to eliminate. Also adds an SSRF surface and retry machinery to solve what a mailbox already solves. Matches hermes-peer's D14. |
| **Injecting live peer capabilities into the system prompt** | Breaks per-conversation prompt caching, which Hermes treats as sacred — every spoke connect/disconnect would invalidate the cache for every open session. Replaced by V3's on-demand discovery. |
| **Hub-side per-spoke tokens** | Option A of four. Closes the "no check at all" gap, but the hub would hold verification material for every spoke — one compromise still commands the fleet, which is the exact property V5 exists to prevent. Cosmetic security. Rejected in favour of spoke-side enforcement (V5a) at comparable cost. |
| **Attenuable capability tokens** (macaroons / biscuits) | Option D of four. Genuinely elegant — unforgeable, with native delegation that would make the MCP-adapter question disappear structurally. Over-engineered for two laptops and one hub. Revisit only if the fleet grows or third-party callers become real. |
| **Queueing tasks for offline spokes** | YAGNI for now (hermes-hub H10). A request to an offline spoke fails fast with a clear error. Revisit only if real usage demands it. Note this is distinct from the *result* mailbox in V12, which is not optional. |

---

## 9. Working agreement for autonomous sessions

Added after a 2026-09-01 audit found an autonomous session had exceeded its
stated scope and recorded a cleanup claim that was false (a tunnel it said it
had torn down was still running hours later).

- Evidence files are a **starting point for verification, not proof**. Gate
  claims get independently reproduced before they are trusted.
- A session that is told not to touch a machine does not touch that machine,
  even if it finds a clever way to.
- Cleanup claims must be verifiable, and get verified.
- The engineering in that session was sound and independently confirmed. The
  problem was scope and self-reporting, not code quality. Both matter.
