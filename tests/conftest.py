"""Suite-wide isolation (BEA-305): the Phase 2.2 in-flight guard persists to
``~/.hermes-hub/inflight.json``; tests must never read or write the real one,
and must not leak guard state between tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_inflight_store(tmp_path, monkeypatch):
    from hermes_hub.tools import inflight

    monkeypatch.setattr(inflight, "store_path", lambda: tmp_path / "inflight-suite.json")
