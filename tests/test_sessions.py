import tempfile
import time
from pathlib import Path

from hermes_hub.sessions import SessionMap, SessionStore


def _fresh_map(tmp_path):
    store = SessionStore(db_path=Path(tmp_path) / "sessions.db")
    return SessionMap(store=store)


def test_same_context_id_reuses_session(tmp_path):
    m = _fresh_map(tmp_path)
    s1 = m.session_for("ctx-a")
    s2 = m.session_for("ctx-a")
    assert s1 == s2


def test_different_context_ids_get_different_sessions(tmp_path):
    m = _fresh_map(tmp_path)
    s1 = m.session_for("ctx-a")
    s2 = m.session_for("ctx-b")
    assert s1 != s2


def test_expired_session_mints_fresh_one(tmp_path):
    store = SessionStore(db_path=Path(tmp_path) / "sessions.db")
    m = SessionMap(store=store, ttl_seconds=0)
    s1 = m.session_for("ctx-a")
    time.sleep(0.01)
    s2 = m.session_for("ctx-a")
    assert s1 != s2


def test_session_persists_across_map_instances(tmp_path):
    db_path = Path(tmp_path) / "sessions.db"
    store1 = SessionStore(db_path=db_path)
    m1 = SessionMap(store=store1)
    s1 = m1.session_for("ctx-a")

    store2 = SessionStore(db_path=db_path)
    m2 = SessionMap(store=store2)
    s2 = m2.session_for("ctx-a")
    assert s1 == s2


def test_no_context_id_falls_back_to_stable_default():
    import tempfile as _tmp

    with _tmp.TemporaryDirectory() as d:
        m = _fresh_map(d)
        s1 = m.session_for("")
        s2 = m.session_for("")
        assert s1 == s2
