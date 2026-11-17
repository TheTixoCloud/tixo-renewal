"""Tests for audit_log.py (v1.8.0 Part 11): append-only event recording and listing."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import audit_log


def _db(tmp_path):
    return audit_log.open_db(str(tmp_path / "audit.sqlite3"))


def test_record_and_list_events(tmp_path):
    conn = _db(tmp_path)
    audit_log.record_event(conn, "admin", "login", detail="ok", remote_addr="127.0.0.1")
    audit_log.record_event(conn, "admin", "user_created", detail="viewer1")
    events = audit_log.list_recent_events(conn)
    assert len(events) == 2
    assert events[0]["action"] == "user_created"  # most recent first
    assert events[1]["action"] == "login"
    assert events[1]["remote_addr"] == "127.0.0.1"


def test_list_recent_events_respects_limit(tmp_path):
    conn = _db(tmp_path)
    for i in range(10):
        audit_log.record_event(conn, "admin", f"action-{i}")
    events = audit_log.list_recent_events(conn, limit=3)
    assert len(events) == 3
    assert events[0]["action"] == "action-9"


def test_empty_log_returns_empty_list(tmp_path):
    conn = _db(tmp_path)
    assert audit_log.list_recent_events(conn) == []


def test_record_event_returns_id(tmp_path):
    conn = _db(tmp_path)
    event_id = audit_log.record_event(conn, "admin", "login")
    assert isinstance(event_id, int)
    assert event_id > 0
