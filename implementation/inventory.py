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
    record_count INTEGER NOT NULL,
    authoritative_total INTEGER
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
    locked TEXT,
    node_serid TEXT,
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

CREATE TABLE IF NOT EXISTS inventory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    vpsid INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    confidence TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_inventory_events_vpsid ON inventory_events(node_id, vpsid);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# COLUMN-LEVEL MIGRATIONS (v1.6.0 fix): CREATE TABLE IF NOT EXISTS only helps brand-new
# databases -- it is a silent no-op against a table that already exists from an older
# version, which is exactly the real bug found in production (v1.5.0 added
# snapshots.authoritative_total in code but never migrated it onto an existing v1.4.0-era
# inventory.sqlite3, crashing inventory-sync the first time it ran post-upgrade). Every
# column ever added after the initial schema must be listed here, idempotently applied via
# PRAGMA table_info existence checks (never relies on catching a "duplicate column"
# exception as the primary mechanism, though that's also safe if this check somehow misses
# a case) -- repeated execution is always a no-op once applied.
COLUMN_MIGRATIONS = (
    ("snapshots", "authoritative_total", "INTEGER"),
    ("snapshot_records", "ip_primary", "TEXT"),
    ("snapshot_records", "ip_count", "TEXT"),
    ("snapshot_records", "ip_list", "TEXT"),
    ("snapshot_records", "runtime_status", "TEXT"),
    ("snapshot_records", "network_suspended_reason", "TEXT"),
)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent and safe to call on every open_db(), on a brand-new database (where the
    CREATE TABLE statements above already include every column, so every check here is a
    no-op) or a database created by any older released version."""
    for table, column, coltype in COLUMN_MIGRATIONS:
        _add_column_if_missing(conn, table, column, coltype)


def open_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    ensure_schema_migrations(conn)
    return conn


def latest_snapshot_id(conn: sqlite3.Connection, node_id: str):
    row = conn.execute(
        "SELECT id FROM snapshots WHERE node_id=? ORDER BY id DESC LIMIT 1", (node_id,)
    ).fetchone()
    return row[0] if row else None


def _load_snapshot_records(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    rows = conn.execute(
        "SELECT vpsid, hostname, status, quota_gb, used_gb, ram_mb, cores, disk_gb, virtualization, "
        "locked, node_serid, ip_primary, ip_count, ip_list, runtime_status, network_suspended_reason "
        "FROM snapshot_records WHERE snapshot_id=?", (snapshot_id,)
    ).fetchall()
    return {r[0]: {"hostname": r[1], "status": r[2], "quota_gb": r[3], "used_gb": r[4],
                   "ram_mb": r[5], "cores": r[6], "disk_gb": r[7], "virtualization": r[8],
                   "locked": r[9], "node_serid": r[10], "ip_primary": r[11], "ip_count": r[12],
                   "ip_list": r[13], "runtime_status": r[14], "network_suspended_reason": r[15]}
            for r in rows}


def ingest_snapshot(conn: sqlite3.Connection, node_id: str, parse_result: dict,
                     captured_at: str = None, ledger_conn=None) -> dict:
    """Ingests the result of virtualizor_info.parse_listvs_response(). Refuses (no-op,
    returns a clear status) if the parse was marked incomplete -- an incomplete snapshot must
    never become the new baseline that future diffs or the renewal engine's evidence depend
    on. Returns a diff against the previous complete snapshot (added/removed/modified/
    migrated), or {"status": "REJECTED_INCOMPLETE", ...} without writing anything.

    `ledger_conn` is an OPTIONAL, separate connection to shadow_monitor.py's renewal ledger
    (read-only use here) -- passing it lets event_history.py upgrade a used_gb-decrease
    observation from POSSIBLE_RESET to CONFIRMED_RESET when a matching APPLIED operation
    exists. Never required, never written to, never merged with this database."""
    if not parse_result.get("complete"):
        return {"status": "REJECTED_INCOMPLETE", "reason": parse_result.get("error"),
                "diff": None}

    authoritative_total = parse_result.get("authoritative_total")
    captured_at = captured_at or utcnow_iso()
    records = parse_result["records"]
    prev_id = latest_snapshot_id(conn, node_id)
    prev_records = _load_snapshot_records(conn, prev_id) if prev_id is not None else {}

    known_before = {}
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO snapshots (node_id, captured_at, record_count, authoritative_total) "
            "VALUES (?, ?, ?, ?)",
            (node_id, captured_at, len(records), authoritative_total),
        )
        snapshot_id = cur.lastrowid
        for r in records:
            import json as _json
            ip_list_raw = r.get("ip_list", "unavailable")
            ip_list_json = _json.dumps(ip_list_raw) if isinstance(ip_list_raw, list) else str(ip_list_raw)
            network_suspended_reason = r.get("network_suspended_reason")
            conn.execute(
                "INSERT INTO snapshot_records (snapshot_id, vpsid, hostname, status, quota_gb, "
                "used_gb, ram_mb, cores, disk_gb, virtualization, locked, node_serid, ip_primary, ip_count, ip_list, "
                "runtime_status, network_suspended_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, r["vpsid"], str(r["hostname"]), str(r["status"]), str(r["quota_gb"]),
                 str(r["used_gb"]), str(r["ram_mb"]), str(r["cores"]), str(r["disk_gb"]),
                 str(r["virtualization"]), str(r.get("locked", "unavailable")),
                 str(r.get("node_serid", "unavailable")), str(r.get("ip_primary", "unavailable")),
                 str(r.get("ip_count", "unavailable")), ip_list_json,
                 str(r.get("runtime_status", "unavailable")),
                 None if network_suspended_reason is None else str(network_suspended_reason)),
            )
            known = conn.execute(
                "SELECT first_seen_at FROM known_vps WHERE node_id=? AND vpsid=?",
                (node_id, r["vpsid"]),
            ).fetchone()
            known_before[r["vpsid"]] = {"first_seen_at": known[0]} if known else None
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

    import event_history as eh_mod
    events = eh_mod.derive_events(diff, lambda vpsid: known_before.get(vpsid), node_id, ledger_conn)
    if events:
        eh_mod.persist_events(conn, node_id, events, captured_at)

    return {"status": "OK", "reason": None, "diff": diff, "snapshot_id": snapshot_id, "events": events}


def get_known_vps(conn: sqlite3.Connection, node_id: str, vpsid: int):
    row = conn.execute(
        "SELECT first_seen_at, last_seen_at, last_seen_quota_gb FROM known_vps WHERE node_id=? AND vpsid=?",
        (node_id, vpsid),
    ).fetchone()
    if row is None:
        return None
    return {"first_seen_at": row[0], "last_seen_at": row[1], "last_seen_quota_gb": row[2]}


# Fields that represent a genuine CONFIGURATION change -- deliberately EXCLUDES used_gb.
# Real-world finding (v1.5.0): including used_gb here caused ~42 of ~60 real VPSs to be
# reported as "modified" on every single sync, purely from normal bandwidth consumption
# ticking up between polls -- a false-positive flood with no actual configuration change
# behind it. Usage is tracked separately below (usage_trend), never mixed into "modified".
CONFIG_CHANGE_FIELDS = ("hostname", "status", "quota_gb", "ram_mb", "cores", "disk_gb",
                        "locked", "node_serid", "ip_primary")


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
    usage_trend = []
    for vpsid in sorted(prev_ids & current_ids):
        old, new = prev[vpsid], current[vpsid]
        changed_fields = {}
        for field in CONFIG_CHANGE_FIELDS:
            old_val = old.get(field)
            new_val = str(new.get(field))
            if old_val is not None and old_val != new_val:
                changed_fields[field] = {"old": old_val, "new": new_val}
        if changed_fields:
            modified.append({"vpsid": vpsid, "changes": changed_fields})

        old_used, new_used = old.get("used_gb"), str(new.get("used_gb"))
        if old_used is not None and old_used != new_used:
            usage_trend.append({"vpsid": vpsid, "old_used_gb": old_used, "new_used_gb": new_used})

    quota_changed = [m for m in modified if "quota_gb" in m["changes"]]
    return {"added": added, "removed": removed, "modified": modified, "usage_trend": usage_trend,
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
    import json as _json
    snap_id = latest_snapshot_id(conn, node_id)
    if snap_id is None:
        return []
    by_id = _load_snapshot_records(conn, snap_id)
    records = [{"vpsid": vpsid, **fields} for vpsid, fields in sorted(by_id.items())]
    for r in records:
        try:
            r["ip_list"] = _json.loads(r["ip_list"])
        except (TypeError, ValueError):
            pass  # leave as the raw stored string (e.g. "unavailable") if not valid JSON
    return records


def last_sync_info(conn: sqlite3.Connection, node_id: str):
    row = conn.execute(
        "SELECT captured_at, record_count, authoritative_total FROM snapshots "
        "WHERE node_id=? ORDER BY id DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return {"captured_at": row[0], "record_count": row[1], "authoritative_total": row[2]}


def get_usage_trend_for_vps(conn: sqlite3.Connection, node_id: str, vpsid: int, limit: int = 60) -> list[dict]:
    """Per-VPS used_gb over time, read straight from the snapshot history already stored by
    every ingest_snapshot() call -- no separate trend-tracking table, no new detection
    logic. Returns oldest-first, capped at `limit` most recent snapshots. Daily/monthly
    aggregation is a presentation concern for the caller (menu.py); this stays a simple,
    honest "what did used_gb read at each observed point in time" series -- never
    interpolated or smoothed, since gaps between observations are real and meaningful
    (see EXPECTED_POLL_INTERVAL_SECONDS-style gap awareness elsewhere in this project)."""
    rows = conn.execute(
        "SELECT s.captured_at, r.used_gb FROM snapshots s "
        "JOIN snapshot_records r ON r.snapshot_id = s.id "
        "WHERE s.node_id=? AND r.vpsid=? ORDER BY s.id DESC LIMIT ?",
        (node_id, vpsid, limit),
    ).fetchall()
    return [{"captured_at": r[0], "used_gb": r[1]} for r in reversed(rows)]
