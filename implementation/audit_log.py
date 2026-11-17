"""Minimal, real audit logging for administrative actions across the Web Control Center
(v1.8.0 Part 11) -- a durable, append-only (rows are never deleted or edited) SQLite table.
Every mutating action taken through the Web UI (login, logout, user management, settings
changes, emergency-grant actions, manual-review resolutions) should call `record_event()`
immediately after it succeeds -- this module never infers an event from other state, it only
records what callers explicitly tell it happened.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    remote_addr TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_time ON audit_events(occurred_at);
"""


def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def record_event(conn: sqlite3.Connection, actor: str, action: str, detail: str = None,
                  remote_addr: str = None, occurred_at: str = None) -> int:
    occurred_at = occurred_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO audit_events (occurred_at, actor, action, detail, remote_addr) "
        "VALUES (?,?,?,?,?)", (occurred_at, actor, action, detail, remote_addr))
    return cur.lastrowid


def list_recent_events(conn: sqlite3.Connection, limit: int = 100) -> list:
    rows = conn.execute(
        "SELECT id, occurred_at, actor, action, detail, remote_addr FROM audit_events "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "occurred_at": r[1], "actor": r[2], "action": r[3], "detail": r[4],
             "remote_addr": r[5]} for r in rows]
