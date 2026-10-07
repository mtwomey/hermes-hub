"""The six ``peer_*`` model tools (W3, M1).

Ported from hermes-peer's ``hermes_peer/tools/peer_tool.py`` per V14 — tool
names, schema shapes, and the output-summarisation discipline all carry over.
What changes:

* **One hub, many spokes.** hermes-peer addressed N peers by config key;
  here every call goes to one hub and names a spoke (H6). "Which peers
  exist" is answered by the hub's live AgentCard, not local config, so a
  spoke that just connected is visible immediately without a config edit.
* **A per-spoke credential (V5a).** ``peer_ask`` attaches the caller's
  opaque secret; the SPOKE checks it, the hub only relays.
* **Task-scoped artifact downloads.** The hub's route is
  ``/a2a/artifacts/{task_id}/{artifact_id}``.
* **Registration is not here.** hermes-peer called ``registry.register()``
  at import time (which is why it needed a ``.pth`` plus a core patch). This
  module only exposes :data:`TOOL_SPECS`; the plugin in ``plugin/`` does the
  registering through ``PluginContext.register_tool()``, so Hermes core is
  never touched.

Output discipline (Task 1.2): every handler returns a JSON **string** with
a compact summary — final text plus ids. Raw ``Task`` JSON, JSON-RPC
envelopes, and protobuf state enums never enter the model's context, and
neither do artifact bytes (only their size and hash).

Credential discipline (Task 1.1, V5a): the credential is opaque. It is
never parsed, never logged, never cached to disk, and never returned in any
handler's output.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_hub.hub_client import TERMINAL_STATES, HubClient, HubClientError, summarize_task
from hermes_hub.tools import inflight

from hermes_hub.caller_contract import (
    ENV_CALLER_CREDENTIAL_PREFIX as ENV_CREDENTIAL_PREFIX,
    ENV_HUB_TOKEN,
    EXTENSION_URI,
    HUB_TOKEN_ACCOUNT,
    KEYCHAIN_SERVICE,
    caller_credential_account,
    caller_credential_env,
)

ENV_HUB_URL = "HERMES_HUB_URL"

DEFAULT_HUB_URL = "http://127.0.0.1:8770"


# -- config / credential resolution ------------------------------------------


def _config_path() -> Path:
    return Path.home() / ".hermes-hub" / "config.json"


def _cache_path() -> Path:
    """Where ``peer_discover`` caches spoke skills.

    Deliberately NOT in the Hermes config: V3 forbids peer state reaching
    the system prompt, and this file is only read by an explicit tool call.
    """
    return Path.home() / ".hermes-hub" / "discovered-peers.json"


def _load_config() -> Dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _configured_hub_url_from_config() -> str:
    return str(_load_config().get("hub_url") or "")


def resolve_hub_url(explicit: str = "") -> str:
    """explicit arg -> ``HERMES_HUB_URL`` -> config file -> built-in default."""
    if explicit:
        return explicit
    env = os.environ.get(ENV_HUB_URL, "")
    if env:
        return env
    from_config = _configured_hub_url_from_config()
    if from_config:
        return from_config
    return DEFAULT_HUB_URL


def resolve_hub_token(explicit: str = "") -> str:
    """The hub's external bearer token (auth question (b) in V5)."""
    if explicit:
        return explicit
    env = os.environ.get(ENV_HUB_TOKEN, "")
    if env:
        return env
    configured = str(_load_config().get("hub_token") or "")
    if configured:
        return configured
    return _keychain_read(HUB_TOKEN_ACCOUNT)


def _keychain_read(account: str) -> str:
    """Best-effort macOS Keychain read; "" on any failure or non-macOS host."""
    if not shutil.which("security"):
        return ""
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").rstrip("\n")


def resolve_peer_credential(spoke_name: str, *, explicit: str = "") -> str:
    """Resolve the caller's opaque per-spoke credential (Task 1.1, V5a).

    Order mirrors ``credentials.py``'s spoke-side resolution:
    explicit arg -> env ``HERMES_HUB_PEER_CREDENTIAL_<SPOKE>`` -> Keychain
    (``hermes-hub`` / ``caller:<spoke>:credential``) -> "" (dev mode).

    Returns opaque bytes-as-str with no assumed structure, so V5b
    (signatures) changes only what the caller puts in here and what the
    spoke checks — never the hub or the wire format.
    """
    if explicit:
        return explicit
    env_key = caller_credential_env(spoke_name)
    env_value = os.environ.get(env_key, "")
    if env_value:
        return env_value
    return _keychain_read(caller_credential_account(spoke_name))


def hub_configured() -> bool:
    """``check_fn`` for the plugin (Task 2.1): hide the tools when no hub is
    configured, mirroring hermes-peer's ``_enabled`` pattern.

    Deliberately cheap and static — it reads env/config only, never the
    network and never live peer state (V3: no per-spoke content may
    influence what the model sees before it asks).
    """
    return bool(os.environ.get(ENV_HUB_URL, "") or _configured_hub_url_from_config())


# -- plumbing ----------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _client(args: Dict[str, Any]) -> HubClient:
    return HubClient(
        hub_url=resolve_hub_url(str(args.get("hub_url") or "")),
        token=resolve_hub_token(str(args.get("hub_token") or "")),
    )


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _arg(args: Dict[str, Any], key: str) -> str:
    return str(args.get(key) or "").strip()


def _spokes_from_card(card: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Group the hub card's namespaced skills back into per-spoke entries.

    The hub namespaces each skill id as ``<spoke>::<skill-id>`` and tags the
    description ``[spoke: <name>] ...`` (see ``agent_card.py``). This undoes
    both so the model sees a clean per-spoke view with real prose (Task 1.3).
    """
    spokes: Dict[str, Dict[str, Any]] = {}
    for name in _connected_names(card):
        spokes.setdefault(name, {"name": name, "skills": []})
    for skill in card.get("skills", []) or []:
        raw_id = str(skill.get("id") or "")
        if "::" not in raw_id:
            continue
        spoke_name, _, skill_id = raw_id.partition("::")
        description = _strip_spoke_prefix(spoke_name, str(skill.get("description") or ""))
        entry = spokes.setdefault(spoke_name, {"name": spoke_name, "skills": []})
        entry["skills"].append(
            {
                "id": skill_id,
                "name": str(skill.get("name") or skill_id),
                "description": description,
                "examples": list(skill.get("examples") or []),
                "tags": [t for t in (skill.get("tags") or []) if not t.startswith("spoke:")],
            }
        )
    return spokes


def _strip_spoke_prefix(spoke_name: str, description: str) -> str:
    """Remove the hub's ``[spoke: <name> ...]`` tag from a skill description.

    Handles both the original ``[spoke: X]`` form and the self-describing
    ``[spoke: X — address with metadata targetSpoke="X"]`` form.
    """
    opener = f"[spoke: {spoke_name}"
    if description.startswith(opener):
        end = description.find("]")
        if end != -1:
            return description[end + 1 :].strip()
    return description


def _connected_names(card: Dict[str, Any]) -> List[str]:
    """Spokes connected right now, including ones advertising no skills.

    Prefers the structured ``connectedSpokes`` param of the hub's routing
    extension; falls back to parsing the legacy description sentence for
    hubs that predate the self-describing card.
    """
    for ext in (card.get("capabilities") or {}).get("extensions", []) or []:
        if ext.get("uri") == EXTENSION_URI:
            names = (ext.get("params") or {}).get("connectedSpokes")
            if isinstance(names, list):
                return [str(n) for n in names if str(n)]
    return _connected_names_from_description(card)


def _connected_names_from_description(card: Dict[str, Any]) -> List[str]:
    """Legacy: parse ``Currently connected: A, B.`` from an old hub's card."""
    description = str(card.get("description") or "")
    marker = "Currently connected: "
    if marker not in description:
        return []
    tail = description.split(marker, 1)[1]
    tail = tail.split(".")[0]
    if "no spokes" in tail:
        return []
    return [part.strip() for part in tail.split(",") if part.strip()]


# -- handlers ----------------------------------------------------------------


def peer_list(args: Dict[str, Any], **_kwargs: Any) -> str:
    """List the spokes connected to the hub right now, with rich skills."""
    try:
        card = _run(_client(args).agent_card())
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    spokes = _spokes_from_card(card)
    return _ok(
        hub=str(card.get("name") or "hermes-hub"),
        peers=[spokes[name] for name in sorted(spokes)],
    )


def peer_info(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Fetch one spoke's identity and skills from the hub's live card."""
    peer_name = _arg(args, "peer_name")
    if not peer_name:
        return _err("peer_name is required")
    try:
        card = _run(_client(args).agent_card())
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    spokes = _spokes_from_card(card)
    peer = spokes.get(peer_name)
    if peer is None:
        available = ", ".join(sorted(spokes)) or "none"
        return _err(
            f"spoke '{peer_name}' is not currently connected to the hub "
            f"(connected: {available})"
        )
    return _ok(peer=peer)


def peer_discover(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Refresh a spoke's skills from the hub and cache them locally.

    The cache is a convenience for later reasoning, never a prompt input
    (V3). Credentials are never written to it.
    """
    peer_name = _arg(args, "peer_name")
    if not peer_name:
        return _err("peer_name is required")
    try:
        card = _run(_client(args).agent_card())
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    spokes = _spokes_from_card(card)
    peer = spokes.get(peer_name)
    if peer is None:
        available = ", ".join(sorted(spokes)) or "none"
        return _err(
            f"spoke '{peer_name}' is not currently connected to the hub "
            f"(connected: {available})"
        )
    try:
        _write_cache(peer_name, peer)
    except OSError as exc:
        return _err(f"could not write discovery cache: {exc}")
    return _ok(peer=peer_name, skills=peer["skills"])


def _write_cache(peer_name: str, peer: Dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cache: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except Exception:
            cache = {}
    # Only skill metadata is persisted — no credentials, no hub token.
    cache[peer_name] = {"name": peer["name"], "skills": peer["skills"]}
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def already_in_progress_instruction(peer_name: str, task_id: str) -> str:
    return (
        f"{peer_name or 'The peer'} is already working on a request from this "
        f"session (task {task_id}); nothing was sent. Use "
        f"peer_wait(task_id=\"{task_id}\") to get that result. Pass "
        f"new_request=true only if this is genuinely a different request."
    )


def _guard_hit(args: Dict[str, Any], session_key: str, peer_name: str) -> Optional[str]:
    """Phase 2.2: return the guard result if this session already has a
    non-terminal task on ``peer_name``; clears stale entries."""
    entry = inflight.get_inflight(session_key, peer_name)
    if not entry:
        return None
    task_id = str(entry.get("task_id") or "")
    try:
        state = summarize_task(_run(_client(args).get_task(task_id)), task_id)["state"]
    except Exception:
        state = "unknown"  # hub unreachable: keep guarding rather than double-send
    if state in TERMINAL_STATES:
        inflight.clear_task(task_id)
        return None
    return _ok(
        state="already_in_progress",
        task_id=task_id,
        original_request=str(entry.get("request") or ""),
        elapsed_s=int(time.time() - float(entry.get("at") or time.time())),
        instruction=already_in_progress_instruction(peer_name, task_id),
    )


def peer_ask(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Send a request to a named spoke through the hub and return its reply."""
    peer_name = _arg(args, "peer_name")
    message = str(args.get("message") or "")
    if not peer_name:
        return _err("peer_name is required")
    if not message.strip():
        return _err("message is required")
    session_key = inflight.caller_session_key(_kwargs)
    if not _truthy(args.get("new_request")):
        guarded = _guard_hit(args, session_key, peer_name)
        if guarded is not None:
            return guarded
    context_id = _arg(args, "context_id") or inflight.last_context(session_key, peer_name)
    credential = resolve_peer_credential(
        peer_name, explicit=str(args.get("credential") or "")
    )
    file_bytes = None
    file_name = ""
    file_path = _arg(args, "file_path")
    if file_path:
        try:
            source = Path(file_path).expanduser()
            file_bytes = source.read_bytes()
            file_name = source.name
        except OSError as exc:
            return _err(f"could not read file_path: {exc}")
    try:
        result = _run(
            _client(args).ask(
                peer_name,
                message,
                context_id=context_id,
                credential=credential,
                file_name=file_name,
                file_bytes=file_bytes,
                wait_seconds=PEER_ASK_WAIT_SECONDS,
            )
        )
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    try:
        inflight.remember_context(session_key, peer_name, str(result.get("context_id") or ""))
        if result.get("state") not in TERMINAL_STATES and result.get("task_id"):
            inflight.set_inflight(
                session_key, peer_name, task_id=str(result["task_id"]), request=message
            )
    except OSError:
        pass  # the guard is best-effort; never fail a delivered ask over it
    return _task_result(result, peer_name=peer_name)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _clear_if_terminal(result: Dict[str, Any]) -> None:
    if result.get("state") in TERMINAL_STATES:
        try:
            inflight.clear_task(str(result.get("task_id") or ""))
        except OSError:
            pass


def still_running_instruction(peer_name: str, task_id: str) -> str:
    where = peer_name or "the peer"
    return (
        f"Still running on {where}. Do NOT send this request again -- the peer "
        f"is already working on it and a repeat would redo the work. Call "
        f"peer_wait(task_id=\"{task_id}\") to keep waiting (up to 270 s per call), "
        f"or peer_status(task_id=\"{task_id}\") to check progress."
    )


def _task_result(result: Dict[str, Any], *, peer_name: str = "") -> str:
    """Model-facing view of a HubClient task summary (ask/wait)."""
    state = result.get("state") or ""
    if state in ("failed", "canceled", "rejected"):
        payload = {
            "success": False,
            "error": result.get("error") or "task failed",
            "state": state,
            "task_id": result.get("task_id", ""),
        }
        if result.get("hermes_error"):
            payload["hermes_error"] = result["hermes_error"]
        return json.dumps(payload, ensure_ascii=False)
    out: Dict[str, Any] = dict(
        state=state,
        text=result.get("text", ""),
        task_id=result.get("task_id", ""),
        context_id=result.get("context_id", ""),
        artifacts=result.get("artifacts", []),
    )
    if state != "completed":
        out["elapsed_s"] = int(result.get("elapsed_s") or 0)
        out["instruction"] = still_running_instruction(peer_name, out["task_id"])
    return _ok(**out)


def clamp_wait_seconds(value: Any) -> float:
    try:
        seconds = float(value) if value not in (None, "") else MAX_WAIT_SECONDS
    except (TypeError, ValueError):
        seconds = MAX_WAIT_SECONDS
    return max(0.0, min(MAX_WAIT_SECONDS, seconds))


def peer_wait(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Phase 1.5: keep waiting (<= 270 s) on a task from an earlier peer_ask."""
    task_id = _arg(args, "task_id")
    if not task_id:
        return _err("task_id is required")
    try:
        result = _run(_client(args).wait(task_id, clamp_wait_seconds(args.get("seconds"))))
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    _clear_if_terminal(result)
    return _task_result(result, peer_name=_arg(args, "peer_name"))


#: Hard cap for peer_wait and peer_ask's client-side wait (D2): below
#: typical tool-call/HTTP ceilings so the caller's turn never hangs.
MAX_WAIT_SECONDS = 270.0
PEER_ASK_WAIT_SECONDS = MAX_WAIT_SECONDS

#: peer_status flags a task as long-running past this age (D5).
LONG_RUNNING_AFTER_S = 600


def _parse_ts(value: Any):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def liveness_summary(metadata: Optional[Dict[str, Any]], now=None) -> Dict[str, Any]:
    """Render hub liveness metadata (startedAt/lastHeartbeatAt) for a model:
    elapsed seconds, seconds since the spoke was last heard, long_running."""
    from datetime import datetime, timezone

    metadata = metadata or {}
    started = _parse_ts(metadata.get("startedAt"))
    if started is None:
        return {}
    now = now or datetime.now(timezone.utc)
    elapsed = int((now - started).total_seconds())
    heard = _parse_ts(metadata.get("lastHeartbeatAt")) or started
    return {
        "elapsed_s": elapsed,
        "last_heard_s_ago": int((now - heard).total_seconds()),
        "long_running": elapsed >= LONG_RUNNING_AFTER_S,
    }


def peer_cancel(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Phase 3.4: cancel a running peer task (A2A CancelTask). The hub tells
    the spoke to interrupt its agent; the task ends ``canceled`` and the
    caller's in-flight guard entry for it is cleared."""
    task_id = _arg(args, "task_id")
    if not task_id:
        return _err("task_id is required")
    try:
        task = _run(_client(args).cancel_task(task_id))
    except HubClientError as exc:
        return _err(f"could not cancel task {task_id}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    try:
        inflight.clear_task(task_id)
    except OSError:
        pass
    status = (task or {}).get("status") or {}
    state = str(status.get("state") or "TASK_STATE_CANCELED")
    state = state[len("TASK_STATE_") :].lower() if state.startswith("TASK_STATE_") else state.lower()
    return _ok(task_id=task_id, state=state)


def peer_status(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Check one task by id. Reads state only — W5 async is out of scope."""
    task_id = _arg(args, "task_id")
    if not task_id:
        return _err("task_id is required")
    try:
        task = _run(_client(args).get_task(task_id))
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    if not task:
        return _err(f"no task found with id {task_id}")
    status = task.get("status") or {}
    message = status.get("message") or {}
    text = "".join(p.get("text", "") for p in message.get("parts", []) or [])
    state = str(status.get("state") or "")
    # Strip the protobuf enum prefix: the model wants "completed", not
    # "TASK_STATE_COMPLETED" (Task 1.2).
    state = state[len("TASK_STATE_") :].lower() if state.startswith("TASK_STATE_") else state.lower()
    resolved_id = str(task.get("id") or task_id)
    from hermes_hub.hub_client import _artifact_from_event

    artifacts = []
    for raw_artifact in task.get("artifacts") or []:
        summary = _artifact_from_event({"artifactUpdate": {"artifact": raw_artifact}})
        if summary is not None:
            summary["task_id"] = resolved_id
            artifacts.append(summary)
    payload: Dict[str, Any] = dict(
        task_id=resolved_id,
        context_id=str(task.get("contextId") or task.get("context_id") or ""),
        state=state,
        text=text,
    )
    if artifacts:
        payload["artifacts"] = artifacts
    if state in ("submitted", "working"):
        # Phase 1.3: elapsed / last-heard / long_running (D5).
        payload.update(liveness_summary(task.get("metadata")))
    return _ok(**payload)


def peer_fetch_artifact(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Download an artifact produced by a spoke task and verify its SHA-256."""
    task_id = _arg(args, "task_id")
    artifact_id = _arg(args, "artifact_id")
    if not artifact_id:
        return _err("artifact_id is required")
    if not task_id:
        return _err(
            "task_id is required: the hub's artifact route is task-scoped "
            "(/a2a/artifacts/{task_id}/{artifact_id}); use the task_id from "
            "the peer_ask result that produced this artifact"
        )
    output_path = _arg(args, "output_path")
    destination = Path(output_path).expanduser() if output_path else None
    try:
        path = _run(
            _client(args).download_artifact(
                task_id,
                artifact_id,
                destination,
                expected_sha256=_arg(args, "sha256"),
            )
        )
    except HubClientError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))
    import hashlib

    data = path.read_bytes()
    return _ok(
        path=str(path),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


# -- schemas -----------------------------------------------------------------

_HUB_URL_PROPERTY = {
    "type": "string",
    "description": (
        "Override the hub base URL. Omit to use the configured hub "
        "(HERMES_HUB_URL or ~/.hermes-hub/config.json)."
    ),
}

PEER_LIST_SCHEMA = {
    "name": "peer_list",
    "description": (
        "List the Hermes peer machines ('spokes') currently reachable through "
        "the hub, with each one's skills and what those skills can do. Call "
        "this first when the user mentions another machine by name, or when a "
        "request might be better answered by a different machine — the result "
        "says which peers are online right now and what each is good for."
    ),
    "parameters": {
        "type": "object",
        "properties": {"hub_url": _HUB_URL_PROPERTY},
    },
}

PEER_INFO_SCHEMA = {
    "name": "peer_info",
    "description": (
        "Fetch one named peer's identity and full skill list, including each "
        "skill's human description and examples. Use when peer_list showed a "
        "peer and you need detail before deciding whether to ask it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer_name": {"type": "string", "description": "Spoke name, e.g. 'Olive'."},
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["peer_name"],
    },
}

PEER_DISCOVER_SCHEMA = {
    "name": "peer_discover",
    "description": (
        "Refresh a peer's advertised skills from the hub and cache them "
        "locally for later reference. Like peer_info, but persists the result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer_name": {"type": "string", "description": "Spoke name, e.g. 'Olive'."},
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["peer_name"],
    },
}

PEER_ASK_SCHEMA = {
    "name": "peer_ask",
    "description": (
        "Ask a named peer machine to do something and return its answer. The "
        "peer runs a full Hermes agent turn with its own tools and local "
        "access, so this is how you reach a file, service, or network that "
        "only that machine can see. Waits up to ~4.5 min (270 s); returns the "
        "peer's final text plus ids for any files it produced. If the peer is "
        "still working at that point the result has state=working and a "
        "task_id: the request is NOT lost and keeps running. Do NOT send the "
        "same request again -- call peer_wait(task_id) or peer_status(task_id)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer_name": {"type": "string", "description": "Spoke name, e.g. 'Olive'."},
            "message": {
                "type": "string",
                "description": "What to ask the peer, in natural language.",
            },
            "context_id": {
                "type": "string",
                "description": (
                    "Reuse a prior context_id to continue the same peer conversation. "
                    "Defaults to this session's last context with that peer."
                ),
            },
            "new_request": {
                "type": "boolean",
                "description": (
                    "Set true ONLY when this is genuinely a different request while an "
                    "earlier peer_ask from this session to the same peer is still "
                    "running; otherwise you get state=already_in_progress and should "
                    "use peer_wait."
                ),
            },
            "file_path": {
                "type": "string",
                "description": "Optional local file to send to the peer along with the message.",
            },
            "credential": {
                "type": "string",
                "description": (
                    "Optional explicit per-peer credential. Normally omitted — "
                    "it is resolved from the environment or Keychain."
                ),
            },
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["peer_name", "message"],
    },
}

PEER_STATUS_SCHEMA = {
    "name": "peer_status",
    "description": (
        "Look up one peer task by id without waiting: state, final text and "
        "artifacts when completed; elapsed_s, last_heard_s_ago and "
        "long_running (10 min+) while still working. Use to re-read the "
        "outcome of an earlier peer_ask."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id from a peer_ask result."},
            "peer_name": {"type": "string", "description": "Spoke name (informational)."},
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["task_id"],
    },
}

PEER_WAIT_SCHEMA = {
    "name": "peer_wait",
    "description": (
        "Keep waiting for a peer task that an earlier peer_ask returned as "
        "state=working. Blocks up to `seconds` (max 270) and returns the final "
        "text when it finishes, or state=working again if it is still running. "
        "Use this instead of re-sending the request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id from the peer_ask result."},
            "seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 270,
                "description": "How long to wait (default and max 270).",
            },
            "peer_name": {"type": "string", "description": "Spoke name (informational)."},
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["task_id"],
    },
}

PEER_CANCEL_SCHEMA = {
    "name": "peer_cancel",
    "description": (
        "Cancel a peer task that is still running (e.g. the request was wrong "
        "or is no longer needed). The peer's agent is interrupted and the task "
        "ends state=canceled; any later result is discarded. Fails if the task "
        "already finished. Tasks also auto-cancel after the hub TTL (30 min)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id from the peer_ask result."},
            "peer_name": {"type": "string", "description": "Spoke name (informational)."},
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["task_id"],
    },
}

PEER_FETCH_ARTIFACT_SCHEMA = {
    "name": "peer_fetch_artifact",
    "description": (
        "Download a file a peer produced, verify its SHA-256, and save it "
        "locally. Both task_id and artifact_id come from the peer_ask result "
        "that produced the file; the download route is task-scoped."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id from the peer_ask result."},
            "artifact_id": {"type": "string", "description": "Artifact id from the peer_ask result."},
            "output_path": {
                "type": "string",
                "description": "Where to write the file. Defaults to the artifact id in the cwd.",
            },
            "sha256": {
                "type": "string",
                "description": "Optional expected SHA-256; the download fails on mismatch.",
            },
            "peer_name": {"type": "string", "description": "Spoke name (informational)."},
            "hub_url": _HUB_URL_PROPERTY,
        },
        "required": ["task_id", "artifact_id"],
    },
}


@dataclass(frozen=True)
class ToolSpec:
    """One registrable tool: everything the plugin needs, and nothing about
    how registration happens."""

    name: str
    schema: Dict[str, Any]
    handler: Callable[..., str]

    @property
    def description(self) -> str:
        return str(self.schema.get("description") or "")


#: The registration surface. The plugin iterates this; nothing here calls
#: into Hermes core (see the module docstring).
TOOL_SPECS: List[ToolSpec] = [
    ToolSpec("peer_list", PEER_LIST_SCHEMA, peer_list),
    ToolSpec("peer_info", PEER_INFO_SCHEMA, peer_info),
    ToolSpec("peer_discover", PEER_DISCOVER_SCHEMA, peer_discover),
    ToolSpec("peer_ask", PEER_ASK_SCHEMA, peer_ask),
    ToolSpec("peer_status", PEER_STATUS_SCHEMA, peer_status),
    ToolSpec("peer_wait", PEER_WAIT_SCHEMA, peer_wait),
    ToolSpec("peer_cancel", PEER_CANCEL_SCHEMA, peer_cancel),
    ToolSpec("peer_fetch_artifact", PEER_FETCH_ARTIFACT_SCHEMA, peer_fetch_artifact),
]

TOOLSET_NAME = "hermes-hub-peer"
