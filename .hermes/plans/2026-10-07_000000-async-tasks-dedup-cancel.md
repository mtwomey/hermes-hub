# Async Tasks, Duplicate Protection and Cancel — Implementation Plan

> **For Hermes:** Implement one phase at a time with TDD. Reproduce every gate
> with real command output; do not accept a sub-agent's self-report for any
> gate. Stop at the end of each phase for review before committing/starting the
> next. Paperclip: BEA-303 (project "Enhance Hermes Hub - timeouts").

**Goal:** A peer request is never lost because the caller stopped waiting, and
a caller model that loses patience does not cause the recipient to redo work.

**Decisions (Matthew, BEA-303 interactions 958f9fc2 + 13bdb291, 2026-10-07):**

| # | Decision |
|---|---|
| D1 | **Always-async hub.** Every task runs to a terminal state independent of any caller connection; result stays fetchable by task id. |
| D2 | **`peer_ask` waits ~4.5 min (270 s)** client-side, then returns `state=working` + task_id + "do NOT re-ask; use peer_wait / peer_status". Normal 1–3 min requests look unchanged. |
| D3 | **Duplicate protection, three layers, no text hashing:** (a) in-flight guard per caller session + peer, (b) recipient-side memory of recent tasks from the same caller, (c) transport dedup on A2A `messageId`. |
| D4 | **A duplicate that arrives while the original runs attaches to the original.** |
| D5 | **`peer_cancel` + automatic stop at 30 min** (hard TTL). `peer_status` shows elapsed time and last heartbeat; flag "long-running" from 10 min. |
| D6 | **Persistence across hub restart is a later phase** (Phase 4). Until then a hub restart loses in-flight and finished results. |
| D7 | **Non-Hermes A2A callers get the same semantics** — contract changes are reflected in the agent card extension + docs/CALLERS.md. |
| D8 | Recipient memory and cancel are in scope. |

---

## 0. Current state (verified 2026-10-07)

| Fact | Evidence |
|---|---|
| `main` == `origin/main` at 7a31e0a; suite 211 passed | `git status -sb`; `.venv/bin/python -m pytest -q` |
| Hub per-task timeout 300 s → task FAILED `hermesError=timeout` | `hub_runtime.py:16`, `hub_executor.py:180-186` |
| On timeout router pops the task queue; later spoke frames silently dropped | `router.py:137-142`, `finally` at end of `route_task`; `dispatch_frame_from_spoke` (`router.py:76-85`) drops frames with no queue |
| Client HTTP read timeout 330 s; `peer_ask` blocks on `SendStreamingMessage` | `hub_client.py:48,152` |
| No cancel frame hub→spoke; spoke agent keeps running | `spoke_executor.py` heartbeat loop ~L318-330; `HubExecutor.cancel` only updates hub task state |
| Task store is `InMemoryTaskStore` | `hub_server.py:24,125` |
| a2a-sdk already supports `configuration.return_immediately` (non-blocking SendMessage) and keeps consuming/persisting events after a streaming client disconnects | `.venv/.../default_request_handler.py:368-400, 447-459` |
| `GetTask` / `CancelTask` JSON-RPC methods are served | `jsonrpc_dispatcher.py:114-120` (per 2026-10-01 plan) |
| Every `peer_ask` mints a new task; a retry without the same `context_id` gets a new spoke session | `hub_client.py:107-225`; `sessions.py` SessionMap |
| Spoke prompt has no knowledge of earlier tasks from the caller | `spoke_executor.py:build_spoke_prompt` |

Implication: the SDK is already async-capable. The defect is entirely our hub
executor/router treating "caller stopped waiting" as "task failed", plus the
client tool having no non-blocking path.

---

## Phase 1 — Stop losing results (always-async core)  [D1, D2, D5-TTL]

**Outcome:** a task that takes 6, 12 or 25 minutes completes in the hub and its
answer is retrievable with `peer_status`; `peer_ask` returns a task id instead of
a failure after ~4.5 min.

1.1 **Hub TTL replaces the 300 s failure timeout.**
   - Rename config to `HERMES_HUB_TASK_TTL_SECONDS` (default 1800); keep
     `HERMES_HUB_TASK_TIMEOUT_SECONDS` as a deprecated alias.
   - `HubExecutor` / `Router.route_task` run until terminal frame or TTL; the
     task never fails merely because a caller disconnected.
   - At TTL: send cancel frame (see 3.x; until Phase 3 lands, mark FAILED with
     `hermesError=ttl_expired` — distinct from today's `timeout`).
   - Tests: fake spoke that answers after (simulated) > old timeout → task
     COMPLETED with text; TTL expiry → `ttl_expired`.

1.2 **Late frames are never dropped silently.**
   - `dispatch_frame_from_spoke`: frame for an unknown task_id is logged at
     WARNING with task_id + frame type (no payload), and counted.
   - Test: frame for unknown task → warning logged, no exception.

1.3 **Liveness metadata on the task.**
   - Record `startedAt`, `lastHeartbeatAt` (from spoke `task_status` frames) in
     task status metadata; `peer_status` renders elapsed and "last heard from
     N s ago"; adds `long_running: true` past 10 min.
   - Tests: heartbeat updates metadata; render at 9 vs 11 min.

1.4 **Client: submit non-blocking, then wait.**
   - `HubClient.submit()` → `SendMessage` with `return_immediately=true`,
     returns task id. `HubClient.wait(task_id, seconds)` → poll `GetTask`
     (or `SubscribeToTask` if available) until terminal or deadline.
   - `HubClient.ask()` = submit + wait(270 s). On deadline returns
     `state=working`, not an error.
   - Tests: fast task returns text in one call; slow task returns working +
     task_id; client HTTP timeout no longer coupled to task duration.

1.5 **Tool surface.**
   - `peer_ask` result on deadline: `{state: "working", task_id, context_id,
     elapsed_s, instruction: "Still running on <peer>. Do NOT send this request
     again. Call peer_wait(task_id) or peer_status(task_id)."}`
   - New `peer_wait(task_id, seconds≤270)`.
   - `peer_status` returns the final text + artifacts when completed.
   - Update plugin SKILL.md / tool descriptions with the "never re-ask" rule.
   - Tests: schema + handler tests in `test_peer_tools.py`.

1.6 **Card/docs (D7):** spoke-routing extension params + `docs/CALLERS.md`
   document: send with `return_immediately`, poll `GetTask`, TTL 1800 s,
   `ttl_expired` error. Conformance test extended.

**Gate 1:** unit suite green; live gate against a real hub + a slow test spoke
(`scripts/gate3_slow_spoke.py` adapted) with TTL shortened via env: a task
longer than the client wait returns `working`, later `peer_status` returns the
real answer; no `timeout` failure anywhere in hub log.

---

## Phase 2 — Duplicate protection  [D3, D4]

2.1 **Transport dedup on `messageId`.**
   - Hub keeps `messageId → task_id` (bounded LRU, TTL ≥ task TTL). A repeat
     SendMessage with a known messageId returns/attaches to the existing task
     instead of dispatching again.
   - `HubClient` always sets a fresh UUID messageId per logical request and
     reuses it on its own transport retries.
   - Tests: duplicate messageId → one spoke dispatch, both callers see same
     task.

2.2 **In-flight guard in the peer plugin.**
   - Track outstanding tasks keyed by (caller session id, peer). If `peer_ask`
     targets a peer with a non-terminal task from the same session, do not
     send; return `{state: "already_in_progress", task_id, original_request
     (first 300 chars), elapsed_s, instruction: "...use peer_wait; pass
     new_request=true only if this is genuinely a different request"}`.
   - `new_request: true` bypasses the guard.
   - Store in `~/.hermes-hub/` (small JSON/SQLite) so it survives a Hermes
     process restart; entries cleared on terminal state or TTL.
   - **Spec item to close first:** confirm how the plugin obtains a stable
     caller-session id from Hermes tool kwargs (fallback: per-process id +
     context_id). Document the choice in this plan before coding.
   - **CLOSED (BEA-305, verified against hermes-agent 1212a7f18ce):**
     `model_tools._execute_tool` builds `dispatch_kwargs = {"task_id",
     "session_id", "user_task"}` and `tools/registry.py dispatch()` passes them
     as `handler(args, **kwargs)` after `_kwargs_accepted_by` signature
     filtering. `peer_ask(args, **_kwargs)` already accepts `**kwargs`, so it
     receives `session_id` (the Hermes conversation session id) and `task_id`
     (subagent/terminal isolation id).
     Caller-session key = `kwargs["session_id"]` when it is a non-empty str;
     otherwise `"proc-<pid>-<boot uuid4>"` minted once per plugin process.
     `task_id` is **not** used (it differs per subagent and is often None for
     the main agent). Guard key = (caller-session key, peer name).
     Known limits, accepted: (1) context compression rotates
     `agent.session_id` (`agent/compression_facade.py`), so a compression
     between two asks starts a new key and the guard misses — layer (b)
     recipient memory and layer (c) messageId still apply; (2) the fallback
     key spans all sessions in one process, so `new_request=true` is the
     escape hatch. The same key drives 2.3 context_id reuse.
   - Tests: second ask during in-flight → guard result, no hub call; with
     `new_request=true` → sent; after completion → sent normally.

2.3 **Same conversation on the recipient.**
   - `peer_ask` defaults `context_id` to the caller session's last context_id
     with that peer (when not given), so follow-ups land in the same spoke
     session. Tests.

2.4 **Recipient-side memory.**
   - Spoke keeps a small ledger (SQLite next to `spoke_sessions.db`): task_id,
     context_id, caller identity, first 300 chars of request, state, first
     500 chars of answer, timestamps; pruned after 24 h.
   - `build_spoke_prompt` appends "Recent requests from this caller (last 2 h)"
     with up to 5 entries and the instruction: "If this request duplicates one
     of these, say so and return/refer to the earlier result instead of redoing
     the work, unless the caller explicitly asks to redo it."
   - Privacy: ledger stays on the spoke; never sent to the hub except as part
     of the answer.
   - Tests: prompt includes ledger entries; pruning; running entry shown as
     "still running (task X)".

2.5 **Card/docs (D7):** messageId dedup semantics in the extension + CALLERS.md.

**Gate 2:** live: model-style double ask (rephrased) to a slow spoke from one
session → one spoke execution (guard); forced `new_request=true` rephrased
duplicate → spoke answer references the earlier task (recipient memory);
duplicate messageId via raw curl → single execution.

---

## Phase 3 — Cancel  [D5]

3.1 **Protocol:** new hub→spoke `task_cancel` frame (protocol.py builder +
   validation).
3.2 **Spoke:** on `task_cancel`, interrupt the running agent. The agent runs in
   `asyncio.to_thread` — **spec item:** determine the Hermes agent interrupt
   API (AIAgent interrupt flag / stop event) before coding; if none exists,
   fall back to "stop forwarding + mark cancelled + discard result" and record
   the residual compute cost honestly. Reply with `task_failed`
   (`cancelled`) or a dedicated `task_cancelled` frame.
   - **CLOSED (BEA-306, verified against hermes-agent 1212a7f18ce):** a real
     cross-thread interrupt API exists. `AIAgent` mixes in
     `agent/interrupt_control.py:InterruptControlMixin`:
     `interrupt(message=None, *, hard_cancel=False, tool_reason=None)` and
     `hard_interrupt(message=None, *, tool_reason=None)` are documented as
     "call from another thread"; they set `_interrupt_requested`, set the
     `_hard_interrupt_requested` event, abort the in-flight model request
     (`_ic_abort_active_request`), signal the per-thread tool interrupt for
     the agent's execution thread and tool workers, and propagate to child
     (delegate) agents. If the interrupt lands before `run_conversation`
     binds its execution thread it is deferred (`_interrupt_thread_signal_
     pending`), so an early cancel is not lost. The supported entry point is
     `agent/interrupt_compat.py:request_hard_interrupt(agent, message,
     tool_reason=...)` — the same call the gateway uses for its TTL and API
     `/stop` (`gateway/run.py:2708`, `gateway/platforms/api_server_runs.py:
     1270`); it falls back to legacy `interrupt()` for stand-ins.
     **Design:** `run_agent_turn` gains an optional `on_agent(agent)` hook
     invoked right after `AIAgent(...)` is built (before `run_conversation`);
     `SpokeExecutor` stores the handle per task_id. On `task_cancel` the
     executor records the task as cancelled, calls
     `request_hard_interrupt(agent, "Cancelled by caller via hub",
     tool_reason="hub cancel")` in a worker thread, logs
     `task <id>: cancel requested, agent interrupted`, sends a dedicated
     terminal `task_cancelled` frame, and suppresses any later
     `task_complete`/`task_failed`/artifact frames for that task (result
     discarded, ledger state `cancelled`). Fallback when no handle is
     registered yet or the runner does not accept `on_agent` (test runners,
     old runners): same stop-forwarding + discard path.
     **Residual cost, stated plainly:** interruption is cooperative. The
     in-flight model HTTP request is aborted, but a tool call already
     executing (e.g. a long terminal command) runs until it next checks the
     interrupt flag or finishes, and tokens already generated are billed.
     The worker thread itself cannot be killed; it exits when
     `run_conversation` returns. The hub never waits on it.
     **Prerequisite found (BEA-306):** `SpokeClient._receive_loop` awaits
     `on_frame(frame)` and `SpokeExecutor.handle_frame` awaits
     `handle_task_frame` for the whole agent turn, so while a task runs the
     spoke reads no further frames — a `task_cancel` would only be seen after
     the task finished (and concurrent tasks to one spoke are serialized
     today). 3.2 must therefore dispatch `task` frames as tracked background
     asyncio tasks (`handle_frame` returns immediately; `_running[task_id]`
     holds the asyncio task + agent handle; exceptions logged), keep
     artifact frames in-order on the receive loop, and handle `task_cancel`
     inline. Tests: cancel received while a slow runner is blocked; second
     task frame is read while the first runs.
3.3 **Hub:** `HubExecutor.cancel` (A2A `CancelTask`) and TTL expiry both send
   `task_cancel`; task ends `CANCELED`.
3.4 **Tool:** `peer_cancel(task_id)`; in-flight guard entry cleared.
3.5 **Card/docs:** CancelTask supported; TTL auto-cancel.

**Gate 3:** live: cancel a running slow task → spoke log shows interrupt, task
`CANCELED`, no late `task_complete`; TTL (shortened) auto-cancels.

---

## Phase 4 — Durable mailbox (later; D6, VISION W5)

4.1 SQLite-backed `TaskStore` in the hub (results survive hub restart).
4.2 "Uncollected results" query (`peer_inbox`) for the caller.
4.3 Spoke outbox: finished results persisted on the spoke and re-sent on
    reconnect until the hub acks (covers spoke WS drop mid-task and hub restart
    while the spoke is working; VISION §7 Q5).
4.4 Decide in-flight-at-hub-restart policy (fail cleanly vs resume via outbox).

Not scheduled until Phases 1–3 are in use; revisit with Matthew.

---

## Out of scope

Push/webhook delivery (VISION V12 forbids); queueing for offline spokes (H10);
MCP adapter (W6); cross-machine credential distribution.

## Risks / open spec items

- Caller-session identity source for the in-flight guard (2.2).
- Hermes agent interrupt API for real cancellation (3.2) — CLOSED (BEA-306): `request_hard_interrupt`; cooperative, residual cost noted in 3.2.
- Spoke WebSocket drop mid-task still loses the result until Phase 4.3; Phase 1
  should at least mark such tasks `failed: spoke_disconnected` promptly rather
  than waiting for TTL.
- Olive (managed Mac, no sudo) must be redeployed for spoke-side changes in
  Phases 2.4 and 3; hub + plugin changes are Pumpkin-only.
