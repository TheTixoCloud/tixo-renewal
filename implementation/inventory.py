"""Read-only VPS inventory snapshots for the dashboard: a SEPARATE SQLite database from
shadow_monitor.py's renewal ledger (`identities`/`operations` tables). This module never
writes to, reads from, or shares a connection with the renewal ledger -- it exists purely to
answer "what does the fleet look like right now, and what changed since last time", never to
decide renewal eligibility (that remains shadow_monitor.py's/engine.py's job alone).

Change tracking is diff-based across two CONSECUTIVE COMPLETE snapshots only. An incomplete
snapshot (see virtualizor_info.parse_listvs_response's `complete` flag) is never ingested as
the new "current" snapshot and never diffed against -- a VPS absent from a partial response
must never be reported as deleted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DEFAULT_DB_PATH = "inventory.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    record_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_records (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    vpsid INTEGER NOT NULL,
    hostname TEXT,
    status TEXT,
    quota_gb TEXT,
    used_gb TEXT,
    ram_mb TEXT,
    cores TEXT,
    disk_gb TEXT,
    virtualization TEXT,
    PRIMARY KEY (snapshot_id, vpsid)
);

CREATE TABLE IF NOT EXISTS known_vps (
    node_id TEXT NOT NULL,
    vpsid INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_quota_gb TEXT,
    PRIMARY KEY (node_id, vpsid)
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def latest_snapshot_id(conn: sqlite3.Connection, node_id: str):
    row = conn.execute(
        "SELECT id FROM snapshots WHERE node_id=? ORDER BY id DESC LIMIT 1", (node_id,)
    ).fetchone()
    return row[0] if row else None


def _load_snapshot_records(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    rows = conn.execute(
        "SELECT vpsid, hostname, status, quota_gb, used_gb, ram_mb, cores, disk_gb, virtualization "
        "FROM snapshot_records WHERE snapshot_id=?", (snapshot_id,)
    ).fetchall()
    return {r[0]: {"hostname": r[1], "status": r[2], "quota_gb": r[3], "used_gb": r[4],
                   "ram_mb": r[5], "cores": r[6], "disk_gb": r[7], "virtualization": r[8]}
            for r in rows}


def ingest_snapshot(conn: sqlite3.Connection, node_id: str, parse_result: dict,
                     captured_at: str = None) -> dict:
    """Ingests the result of virtualizor_info.parse_listvs_response(). Refuses (no-op,
    returns a clear status) if the parse was marked incomplete -- an incomplete snapshot must
    never become the new baseline that future diffs or the renewal engine's evidence depend
    on. Returns a diff against the previous complete snapshot (added/removed/modified/
    migrated), or {"status": "REJECTED_INCOMPLETE", ...} without writing anything."""
    if not parse_result.get("complete"):
        return {"status": "REJECTED_INCOMPLETE", "reason": parse_result.get("error"),
                "diff": None}

    captured_at = captured_at or utcnow_iso()
    records = parse_result["records"]
    prev_id = latest_snapshot_id(conn, node_id)
    prev_records = _load_snapshot_records(conn, prev_id) if prev_id is not None else {}

    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO snapshots (node_id, captured_at, record_count) VALUES (?, ?, ?)",
            (node_id, captured_at, len(records)),
        )
        snapshot_id = cur.lastrowid
        for r in records:
            conn.execute(
                "INSERT INTO snapshot_records (snapshot_id, vpsid, hostname, status, quota_gb, "
                "used_gb, ram_mb, cores, disk_gb, virtualization) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, r["vpsid"], str(r["hostname"]), str(r["status"]), str(r["quota_gb"]),
                 str(r["used_gb"]), str(r["ram_mb"]), str(r["cores"]), str(r["disk_gb"]),
                 str(r["virtualization"])),
            )
            known = conn.execute(
                "SELECT first_seen_at FROM known_vps WHERE node_id=? AND vpsid=?",
                (node_id, r["vpsid"]),
            ).fetchone()
            if known is None:
                conn.execute(
                    "INSERT INTO known_vps (node_id, vpsid, first_seen_at, last_seen_at, last_seen_quota_gb) "
                    "VALUES (?,?,?,?,?)",
                    (node_id, r["vpsid"], captured_at, captured_at, str(r["quota_gb"])),
                )
            else:
                conn.execute(
                    "UPDATE known_vps SET last_seen_at=?, last_seen_quota_gb=? WHERE node_id=? AND vpsid=?",
                    (captured_at, str(r["quota_gb"]), node_id, r["vpsid"]),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    current_by_id = {r["vpsid"]: r for r in records}
    diff = _diff_snapshots(prev_records, current_by_id)
    return {"status": "OK", "reason": None, "diff": diff, "snapshot_id": snapshot_id}


def _diff_snapshots(prev: dict, current: dict) -> dict:
    """Pure diff between two already-validated-complete snapshots (dicts keyed by vpsid).
    `prev` empty means this is the first-ever snapshot for this node -- everything in
    `current` is reported as added, nothing as removed (never flag a first sync as mass
    deletion)."""
    prev_ids = set(prev.keys())
    current_ids = set(current.keys())

    added = sorted(current_ids - prev_ids)
    removed = sorted(prev_ids - current_ids)
    modified = []
    for vpsid in sorted(prev_ids & current_ids):
        old, new = prev[vpsid], current[vpsid]
        changed_fields = {}
        for field in ("hostname", "status", "quota_gb", "used_gb", "ram_mb", "cores", "disk_gb"):
            old_val = old.get(field)
            new_val = str(new.get(field))
            if old_val is not None and old_val != new_val:
                changed_fields[field] = {"old": old_val, "new": new_val}
        if changed_fields:
            modified.append({"vpsid": vpsid, "changes": changed_fields})

    quota_changed = [m for m in modified if "quota_gb" in m["changes"]]
    return {"added": added, "removed": removed, "modified": modified,
            "quota_changed": [m["vpsid"] for m in quota_changed],
            "added_count": len(added), "removed_count": len(removed),
            "modified_count": len(modified)}


def detect_id_reuse(conn: sqlite3.Connection, node_id: str, vpsid: int, observed_quota_gb) -> bool:
    """Heuristic ID-reuse flag: a vpsid that was previously REMOVED from the fleet (absent from
    at least one complete snapshot) and later reappears with a materially different quota is
    flagged for operator review -- never silently treated as the same long-running VPS, and
    never auto-fed into the renewal engine's eligibility evidence (that stays in
    shadow_monitor.py, which performs its own separate created_epoch-based conflict check)."""
    row = conn.execute(
        "SELECT last_seen_quota_gb FROM known_vps WHERE node_id=? AND vpsid=?", (node_id, vpsid)
    ).fetchone()
    if row is None or row[0] is None:
        return False
    try:
        prev_quota = Decimal(row[0])
        new_quota = Decimal(str(observed_quota_gb))
    except Exception:
        return False
    return prev_quota != new_quota


def latest_records(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    snap_id = latest_snapshot_id(conn, node_id)
    if snap_id is None:
        return []
    by_id = _load_snapshot_records(conn, snap_id)
    return [{"vpsid": vpsid, **fields} for vpsid, fields in sorted(by_id.items())]


def last_sync_info(conn: sqlite3.Connection, node_id: str):
    row = conn.execute(
        "SELECT captured_at, record_count FROM snapshots WHERE node_id=? ORDER BY id DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return {"captured_at": row[0], "record_count": row[1]}
