"""Phase 2.2 / 2.3 (BEA-305): caller-side in-flight guard and context reuse.

Keyed by (caller-session key, peer name). The caller-session key is the
Hermes ``session_id`` tool kwarg when present, else a per-process id (spec
item closed in the plan, Phase 2.2). State lives in a small JSON file under
``~/.hermes-hub/`` so it survives a Hermes process restart. Only task ids,
context ids, timestamps and the first 300 chars of the request are stored --
never credentials or tokens.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

#: Entries older than this are ignored and pruned (matches the hub task TTL).
ENTRY_TTL_SECONDS = 1800.0
CONTEXT_TTL_SECONDS = 86400.0
REQUEST_PREVIEW_CHARS = 300

_PROCESS_KEY = f"proc-{os.getpid()}-{uuid.uuid4().hex}"


def store_path() -> Path:
    return Path.home() / ".hermes-hub" / "inflight.json"


def caller_session_key(kwargs: Mapping[str, Any]) -> str:
    session_id = kwargs.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return _PROCESS_KEY


def _key(session_key: str, peer_name: str) -> str:
    return f"{session_key}\x1f{peer_name}"


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("inflight", {})
            data.setdefault("contexts", {})
            return data
    except (OSError, ValueError):
        pass
    return {"inflight": {}, "contexts": {}}


def _save(data: Dict[str, Any]) -> None:
    now = time.time()
    ttl = {"inflight": ENTRY_TTL_SECONDS, "contexts": CONTEXT_TTL_SECONDS}
    for section, limit in ttl.items():
        data[section] = {
            k: v for k, v in data[section].items() if now - float(v.get("at", 0)) <= limit
        }
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def get_inflight(session_key: str, peer_name: str) -> Optional[Dict[str, Any]]:
    entry = _load()["inflight"].get(_key(session_key, peer_name))
    if not entry or time.time() - float(entry.get("at", 0)) > ENTRY_TTL_SECONDS:
        return None
    return entry


def set_inflight(session_key: str, peer_name: str, *, task_id: str, request: str) -> None:
    data = _load()
    data["inflight"][_key(session_key, peer_name)] = {
        "task_id": task_id,
        "request": request[:REQUEST_PREVIEW_CHARS],
        "at": time.time(),
    }
    _save(data)


def clear_task(task_id: str) -> None:
    if not task_id:
        return
    data = _load()
    before = len(data["inflight"])
    data["inflight"] = {k: v for k, v in data["inflight"].items() if v.get("task_id") != task_id}
    if len(data["inflight"]) != before:
        _save(data)


def last_context(session_key: str, peer_name: str) -> str:
    entry = _load()["contexts"].get(_key(session_key, peer_name))
    return str(entry.get("context_id") or "") if entry else ""


def remember_context(session_key: str, peer_name: str, context_id: str) -> None:
    if not context_id:
        return
    data = _load()
    data["contexts"][_key(session_key, peer_name)] = {"context_id": context_id, "at": time.time()}
    _save(data)
