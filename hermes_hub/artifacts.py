"""Hub-side artifact storage and verification (Task 2.1, W2, V9/V14).

Ported from hermes-peer's ``hermes_peer/artifacts.py`` per V14 (port working
code, don't reimplement): storage layout, SHA-256 hashing, and the
inline/URL size threshold all carry over directly.

**What does NOT port** (V14's explicit warning): hermes-peer serves large
artifacts from an authenticated URL on the *producing* machine. In
hub-and-spoke, spokes have no listener (V2) -- that cannot work here. Bytes
travel spoke -> hub over the WebSocket in chunked frames (see
``protocol.py``'s ``FRAME_ARTIFACT_*``), and **the hub** is what serves the
download URL, not the spoke. This module only owns storage/hashing/naming;
the transport is chunked_reassembly.py-equivalent logic living in
``router.py``.

Storage keys by ``task_id`` rather than hermes-peer's ``request_id`` -- the
equivalent unit of work in this protocol.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

#: V14 provenance: this module is ported from hermes-peer's artifacts.py.
PORTED_FROM = "hermes-peer/hermes_peer/artifacts.py"

#: Matches hermes-peer's max_inline_bytes (D9 / Task 2.2 of this plan).
INLINE_MAX_BYTES = 65536

#: Where the hub serves stored artifacts for download (Task 2.4).
ARTIFACT_DOWNLOAD_PATH = "/a2a/artifacts"


@dataclass(slots=True)
class ArtifactRef:
    artifact_id: str
    name: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str = ""
    task_id: str = ""
    path: str = ""
    created_at: str = ""
    expires_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=str(data.get("artifact_id") or ""),
            name=str(data.get("name") or ""),
            mime_type=str(data.get("mime_type") or "application/octet-stream"),
            size_bytes=int(data.get("size_bytes") or 0),
            sha256=str(data.get("sha256") or ""),
            task_id=str(data.get("task_id") or ""),
            path=str(data.get("path") or ""),
            created_at=str(data.get("created_at") or ""),
            expires_at=data.get("expires_at") or None,
        )


def _hub_home() -> Path:
    return Path.home() / ".hermes-hub"


def _artifact_root() -> Path:
    root = _hub_home() / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def store_artifact_bytes(
    *,
    task_id: str,
    name: str,
    data: bytes,
    mime_type: str = "application/octet-stream",
    expires_at: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> ArtifactRef:
    artifact_id = artifact_id or f"art_{uuid4().hex[:12]}"
    root = _artifact_root() / task_id
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{artifact_id}_{Path(name).name}"
    path = root / filename
    path.write_bytes(data)
    ref = ArtifactRef(
        artifact_id=artifact_id,
        name=Path(name).name,
        mime_type=mime_type,
        size_bytes=len(data),
        sha256=_sha256_bytes(data),
        task_id=task_id,
        path=str(path),
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires_at,
    )
    _write_metadata(root, ref)
    return ref


def store_artifact_file(
    *,
    task_id: str,
    path: Path,
    mime_type: str = "application/octet-stream",
    expires_at: Optional[str] = None,
) -> ArtifactRef:
    data = path.read_bytes()
    return store_artifact_bytes(
        task_id=task_id,
        name=path.name,
        data=data,
        mime_type=mime_type,
        expires_at=expires_at,
    )


def fetch_artifact_file(artifact: ArtifactRef, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(artifact.path), destination)
    digest = _sha256_file(destination)
    if artifact.sha256 and digest != artifact.sha256:
        raise ValueError(
            f"SHA-256 mismatch for {artifact.name}: expected {artifact.sha256}, got {digest}"
        )
    return destination


def load_artifact_metadata(task_id: str, artifact_id: str) -> Optional[ArtifactRef]:
    meta = _artifact_root() / task_id / "metadata.json"
    if not meta.exists():
        return None
    payload = json.loads(meta.read_text(encoding="utf-8"))
    for item in payload.get("artifacts", []):
        if item.get("artifact_id") == artifact_id:
            return ArtifactRef.from_dict(item)
    return None


def _write_metadata(root: Path, artifact: ArtifactRef) -> None:
    meta = root / "metadata.json"
    payload: Dict[str, Any] = {"artifacts": []}
    if meta.exists():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8")) or payload
        except Exception:
            payload = {"artifacts": []}
    artifacts = [
        item for item in payload.get("artifacts", []) if item.get("artifact_id") != artifact.artifact_id
    ]
    artifacts.append(artifact.to_dict())
    meta.write_text(json.dumps({"artifacts": artifacts}, indent=2, ensure_ascii=False), encoding="utf-8")
