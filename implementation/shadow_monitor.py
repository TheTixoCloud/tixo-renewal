#!/usr/bin/env python3
"""Minimal local-only shadow monitor for the Virtualizor bandwidth-carryover rule.

Reads production over SSH (strictly read-only: it only ever executes the ALREADY-VETTED,
read-only `virt-creation-times.py` -- a pure SQL SELECT with no write capability, inspected in
Phase 1/this review). It NEVER writes to production, never touches a production file, never
runs any mutating script. All durable state lives in a local SQLite database on THIS server.

Reuses implementation/engine.py's CarryoverEngine unchanged -- this file is a thin adapter
(SSH polling + SQLite persistence + reporting), not a reimplementation of the accounting
logic, per the instruction not to refactor the existing v4 engine.

Usage:
    python3 shadow_monitor.py --poll      # one read-only SSH poll + ingest + report
    python3 shadow_monitor.py --report    # report from existing local state only, no SSH
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from engine import CarryoverEngine, check_monthly_anniversary, renewal_period_index
from models import Identity, Event, to_decimal

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "shadow-monitor-state", "shadow_monitor.sqlite3")
# This package ships with NO real host baked in anywhere. fetch_live_rows() always requires
# an explicit ssh_cmd from a real nodes.json entry (see nodes.py) -- there is deliberately no
# usable hardcoded fallback here, so cloning this repo can never accidentally point at anyone
# else's server.
SSH_CMD = None
# Expected poll cadence. A gap larger than 3x this between two consecutive snapshots for the
# same vpsid means a pre-reset snapshot may have been missed -- flagged, never assumed fine.
EXPECTED_POLL_INTERVAL_SECONDS = 1800


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- SQLite schema (atomic transactions, durable across restarts) ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    vpsid INTEGER PRIMARY KEY,
    created_epoch INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vpsid INTEGER NOT NULL,
    stored_created_epoch INTEGER NOT NULL,
    observed_created_epoch INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vpsid INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    quota_gb TEXT NOT NULL,
    used_gb TEXT NOT NULL,
    created_epoch INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_vpsid_time ON snapshots(vpsid, captured_at);

CREATE TABLE IF NOT EXISTS renewal_checkpoints (
    period_key TEXT PRIMARY KEY,
    vpsid INTEGER NOT NULL,
    created_epoch INTEGER NOT NULL,
    period_index INTEGER NOT NULL,
    granted_event_signature TEXT NOT NULL,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_checkpoints (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at TEXT,
    last_poll_row_count INTEGER,
    first_poll_at TEXT
);
"""


def open_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transaction control below
    conn.execute("PRAGMA journal_mode=WAL;")  # durable, crash-safe commits
    conn.executescript(SCHEMA)
    import writer as _writer  # local import avoids a hard dependency cycle at module load time
    _writer.ensure_operations_schema(conn)
    return conn


# ---------- Production read-only fetch (the ONLY production touchpoint) ----------

def fetch_live_rows(ssh_cmd: list[str] = None) -> list[dict]:
    """Executes the EXISTING, already-vetted read-only production script
    `/root/virt-creation-times.py` over SSH -- a single `SELECT ... FROM vps` with no write
    capability (re-inspected in this review: no INSERT/UPDATE/DELETE anywhere in that file).
    No new script is written to production; nothing on disk there changes.

    ssh_cmd: the SSH invocation (list of argv tokens) for the TARGET NODE, ending just before
    the remote command. There is NO usable default -- a real one must always come from a
    nodes.json entry (see nodes.py). Multi-node callers (renewal_cli.py) pass each node's own
    ssh_cmd -- this function itself has no node-count limit or hardcoded host of any kind.
    """
    ssh_cmd = ssh_cmd or SSH_CMD
    if not ssh_cmd:
        raise RuntimeError("no ssh_cmd provided and no default configured -- set up "
                            "renewal-config/nodes.json with a real host first")
    result = subprocess.run(
        ssh_cmd + ["python3", "virt-creation-times.py"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH read-only fetch failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    if not lines or not lines[0].startswith("vpsid"):
        raise RuntimeError(f"Unexpected output from virt-creation-times.py: {result.stdout[:200]!r}")
    rows = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        vpsid, uid, created_unix, created_at, created_day, edittime, bandwidth, used_bandwidth = parts
        rows.append({
            "vpsid": int(vpsid),
            "created_epoch": int(created_unix),
            "quota_gb": to_decimal(bandwidth),
            "used_gb": to_decimal(used_bandwidth),
        })
    return rows


# ---------- Ingestion (atomic per poll) ----------

def ingest_poll(conn: sqlite3.Connection, rows: list[dict], as_of: str) -> dict:
    """One atomic transaction: record identities (durable, never silently overwritten on
    conflict), append a snapshot per vpsid, and update the monitor checkpoint. A restart of
    this process simply re-opens the same SQLite file -- no renewal period can ever be
    double-granted or forgotten because none of this state lives in process memory alone."""
    conflicts = []
    conn.execute("BEGIN")
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT created_epoch FROM identities WHERE vpsid=?", (row["vpsid"],)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO identities (vpsid, created_epoch, first_seen_at) VALUES (?,?,?)",
                    (row["vpsid"], row["created_epoch"], as_of),
                )
            elif existing[0] != row["created_epoch"]:
                # Fail closed: never silently adopt a changed creation epoch -- this could be
                # a VPS-ID-reuse event or a data anomaly. Keep the original, record the
                # conflict, let the reporting layer flag it FROZEN_FOR_REVIEW.
                conflicts.append((row["vpsid"], existing[0], row["created_epoch"]))
                conn.execute(
                    "INSERT INTO identity_conflicts (vpsid, stored_created_epoch, "
                    "observed_created_epoch, observed_at) VALUES (?,?,?,?)",
                    (row["vpsid"], existing[0], row["created_epoch"], as_of),
                )
            conn.execute(
                "INSERT INTO snapshots (vpsid, captured_at, quota_gb, used_gb, created_epoch) "
                "VALUES (?,?,?,?,?)",
                (row["vpsid"], as_of, str(row["quota_gb"]), str(row["used_gb"]), row["created_epoch"]),
            )
        conn.execute(
            "INSERT INTO monitor_checkpoints (id, last_poll_at, last_poll_row_count, first_poll_at) "
            "VALUES (1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_poll_at=excluded.last_poll_at, "
            "last_poll_row_count=excluded.last_poll_row_count",
            # first_poll_at is set ONLY on the very first insert -- the ON CONFLICT clause
            # deliberately does not touch it, so it is a durable "when did we start watching"
            # marker used to refuse retroactive initial grants for VPS that already existed
            # before this software was ever installed (see policy_integration.py).
            (as_of, len(rows), as_of),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"rows_ingested": len(rows), "identity_conflicts": conflicts}


# ---------- Detection: reuses CarryoverEngine, never reimplements its logic ----------

def _load_granted_periods(conn: sqlite3.Connection) -> dict:
    out = {}
    for period_key, vpsid, created_epoch, period_index, eid in conn.execute(
        "SELECT period_key, vpsid, created_epoch, period_index, granted_event_signature "
        "FROM renewal_checkpoints"
    ):
        out[period_key] = eid
    return out


def _persist_granted_period(conn: sqlite3.Connection, period_key: str, vpsid: int,
                             created_epoch: int, period_index: int, eid: str, status: str, as_of: str):
    conn.execute("BEGIN")
    conn.execute(
        "INSERT OR REPLACE INTO renewal_checkpoints "
        "(period_key, vpsid, created_epoch, period_index, granted_event_signature, status, recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (period_key, vpsid, created_epoch, period_index, eid, status, as_of),
    )
    conn.execute("COMMIT")


def detect_renewal_candidates(conn: sqlite3.Connection, node_id: str = None,
                               policy_config=None) -> list[dict]:
    """For every vpsid with at least two snapshots, build a synthetic USAGE_DROP_REVIEW-style
    Event from the two most recent snapshots and feed it through the EXISTING, unmodified
    CarryoverEngine -- same fail-closed formula, same calendar-anniversary eligibility check,
    same duplicate-renewal-period guard as the v4 engine already implements and tests cover.

    This function's own job is only: (1) detect a missing pre-reset snapshot (a gap far wider
    than the expected poll interval) and flag it, (2) detect an unverified/conflicted identity
    and flag it, (3) hand everything else to the engine and map every non-actionable outcome
    to FROZEN_FOR_REVIEW in the report, per the explicit task requirement.

    node_id/policy_config: when BOTH are given, the reported `proposed_new_quota_gb` for a
    PLAUSIBLE candidate is recomputed via the operator's actual resolved policy (per-VPS/
    per-host monthly_grant_gb/carryover overrides), not the engine's fixed 100GB reference
    formula -- this is a real fix, not cosmetic: apply_due() already uses
    policy_integration.preview_renewal() (policy-aware) to decide what it actually writes, but
    this reporting path previously always showed the fixed-100GB number regardless of policy,
    which would silently mislead an operator checking dry-run/status before enabling
    auto_renewal_enabled with a non-default monthly base. Without both args, behavior is
    unchanged (same fixed-100GB reference value as before -- used for historical/audit
    contexts that have no policy config at all, e.g. replay.py)."""
    vpsids = [r[0] for r in conn.execute("SELECT DISTINCT vpsid FROM snapshots")]
    granted_periods = _load_granted_periods(conn)
    verified_epochs = {
        vpsid: epoch for vpsid, epoch in conn.execute("SELECT vpsid, created_epoch FROM identities")
    }
    conflicted_vpsids = {
        row[0] for row in conn.execute("SELECT DISTINCT vpsid FROM identity_conflicts")
    }

    engine = CarryoverEngine(fixed_reset_keys=set(), verified_created_epochs=verified_epochs,
                              historical_mode=False)
    engine.granted_renewal_periods.update(granted_periods)

    results = []
    for vpsid in vpsids:
        if vpsid in conflicted_vpsids:
            results.append({
                "vpsid": vpsid, "status": "FROZEN_FOR_REVIEW",
                "reason": "uncertain-identity: observed creation epoch conflicts with a "
                          "previously recorded value for this vpsid -- never silently resolved",
            })
            continue

        rows = list(conn.execute(
            "SELECT captured_at, quota_gb, used_gb FROM snapshots WHERE vpsid=? "
            "ORDER BY captured_at DESC LIMIT 2", (vpsid,)
        ))
        if len(rows) < 2:
            results.append({
                "vpsid": vpsid, "status": "FROZEN_FOR_REVIEW",
                "reason": "missing-pre-reset-snapshot: fewer than two observations recorded yet "
                          "for this vpsid -- nothing to compare",
            })
            continue

        (curr_ts, curr_q, curr_u), (prev_ts, prev_q, prev_u) = rows
        gap_seconds = _iso_gap_seconds(prev_ts, curr_ts)
        if gap_seconds is not None and gap_seconds > 3 * EXPECTED_POLL_INTERVAL_SECONDS:
            results.append({
                "vpsid": vpsid, "status": "FROZEN_FOR_REVIEW",
                "reason": f"missing-pre-reset-snapshot: {gap_seconds}s gap between consecutive "
                          f"snapshots, far exceeding the expected {EXPECTED_POLL_INTERVAL_SECONDS}s "
                          f"poll interval -- a reset could have happened in the gap unobserved",
            })
            continue

        created_epoch = verified_epochs[vpsid]
        ev = Event(
            type="USAGE_DROP_REVIEW", identity=Identity(vpsid=vpsid, created=str(created_epoch)),
            before_ts=prev_ts, after_ts=curr_ts,
            old_quota_gb=to_decimal(prev_q), new_quota_gb=to_decimal(curr_q),
            old_used_gb=to_decimal(prev_u), new_used_gb=to_decimal(curr_u),
        )
        engine.ingest_event(ev)

    for p in engine.get_proposals():
        if p.kind == "MONTHLY_RENEWAL" and p.status == "READY_FOR_HUMAN_APPLICATION":
            proposed = p.proposed_new_quota_gb
            policy_resolved = False
            policy_note = None
            if node_id is not None and policy_config is not None:
                import policy as _pol  # local import: shadow_monitor must stay importable
                resolved = _pol.resolve_policy(policy_config, node_id, p.vpsid)
                proposed = _pol.compute_renewal_quota(resolved, p.old_quota_gb, p.old_used_gb)
                policy_resolved = True
                if proposed != p.proposed_new_quota_gb:
                    policy_note = (f"policy-resolved monthly_grant_gb={resolved.monthly_grant_gb} "
                                    f"differs from the fixed 100GB reference formula "
                                    f"({p.proposed_new_quota_gb} GB) -- this value, not that one, "
                                    f"is what apply-due would actually write")
            results.append({
                "vpsid": p.vpsid, "status": "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION",
                "reason": p.reasons[0], "proposed_new_quota_gb": str(proposed),
                "renewal_period_key": p.renewal_period_key, "policy_resolved": policy_resolved,
                **({"policy_note": policy_note} if policy_note else {}),
            })
            if p.renewal_period_key:
                period_index = renewal_period_index(verified_epochs[p.vpsid], p.reset_after_ts)
                _persist_granted_period(conn, p.renewal_period_key, p.vpsid,
                                         verified_epochs[p.vpsid], period_index, p.event_id,
                                         "FLAGGED", utcnow_iso())
        else:
            # Everything else -- unverified eligibility, duplicate renewal period, ambiguous
            # quota overlap, not-a-renewal, etc. -- is reported as FROZEN_FOR_REVIEW per the
            # explicit task requirement. Never silently dropped.
            results.append({
                "vpsid": p.vpsid, "status": "FROZEN_FOR_REVIEW",
                "reason": p.reasons[0] if p.reasons else p.status, "engine_status": p.status,
            })

    return results


def _iso_gap_seconds(ts1: str, ts2: str):
    try:
        d1 = datetime.fromisoformat(ts1)
        d2 = datetime.fromisoformat(ts2)
    except ValueError:
        return None
    return abs((d2 - d1).total_seconds())


# ---------- Reporting ----------

def next_anniversary_report(conn: sqlite3.Connection) -> list[dict]:
    """Requirement #4: show the expected monthly anniversary for each VPS, using only the
    verified real creation epoch (never an inferred timestamp)."""
    out = []
    now = datetime.now(timezone.utc)
    for vpsid, created_epoch in conn.execute("SELECT vpsid, created_epoch FROM identities ORDER BY vpsid"):
        created_dt = datetime.fromtimestamp(created_epoch, tz=timezone.utc)
        # Next anniversary: same day-of-month (clamped) in the first future month.
        year, month = now.year, now.month
        import calendar as _cal
        day = min(created_dt.day, _cal.monthrange(year, month)[1])
        candidate = datetime(year, month, day, created_dt.hour, created_dt.minute, tzinfo=timezone.utc)
        if candidate <= now:
            month += 1
            if month > 12:
                month = 1
                year += 1
            day = min(created_dt.day, _cal.monthrange(year, month)[1])
            candidate = datetime(year, month, day, created_dt.hour, created_dt.minute, tzinfo=timezone.utc)
        out.append({
            "vpsid": vpsid, "created_at": created_dt.isoformat(),
            "next_expected_anniversary": candidate.isoformat(),
        })
    return out


def print_report(conn: sqlite3.Connection, detections: list[dict]):
    print("=== Shadow monitor report (local-only, production never modified) ===")
    checkpoint = conn.execute("SELECT last_poll_at, last_poll_row_count FROM monitor_checkpoints WHERE id=1").fetchone()
    if checkpoint:
        print(f"Last poll: {checkpoint[0]} ({checkpoint[1]} VPS observed)")
    print(f"\nTracked identities: {conn.execute('SELECT COUNT(*) FROM identities').fetchone()[0]}")
    print(f"Identity conflicts recorded: {conn.execute('SELECT COUNT(*) FROM identity_conflicts').fetchone()[0]}")
    print(f"Granted renewal periods on record: {conn.execute('SELECT COUNT(*) FROM renewal_checkpoints').fetchone()[0]}")

    print("\n--- Next expected monthly anniversary per VPS ---")
    for row in next_anniversary_report(conn):
        print(f"  vpsid={row['vpsid']:<4} created={row['created_at']}  next_anniversary={row['next_expected_anniversary']}")

    print("\n--- Detection results ---")
    for d in detections:
        print(f"  vpsid={d['vpsid']:<4} status={d['status']:<45} reason={d.get('reason', '')}")
        if d.get("proposed_new_quota_gb") is not None:
            print(f"      proposed_new_quota_gb={d['proposed_new_quota_gb']} "
                  f"(policy-resolved: {'yes' if d.get('policy_resolved') else 'no -- fixed 100GB reference'})")
        if d.get("policy_note"):
            print(f"      NOTE: {d['policy_note']}")


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", action="store_true", help="Fetch a live read-only snapshot over SSH, ingest, detect, report")
    ap.add_argument("--report", action="store_true", help="Report from existing local state only, no SSH")
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    conn = open_db(args.db)
    if args.poll:
        rows = fetch_live_rows()
        as_of = utcnow_iso()
        ingest_result = ingest_poll(conn, rows, as_of)
        print(f"Ingested {ingest_result['rows_ingested']} rows at {as_of} "
              f"({len(ingest_result['identity_conflicts'])} identity conflicts)")
    detections = detect_renewal_candidates(conn)
    print_report(conn, detections)
    conn.close()


if __name__ == "__main__":
    main()
