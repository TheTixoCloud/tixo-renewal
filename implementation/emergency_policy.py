"""Exhausted-bandwidth-with-no-scheduled-grant-due policy (v1.8.0).

CONTEXT: engine.py/policy.py/shadow_monitor.py already handle the REGULAR, scheduled monthly
renewal grant (and its own idempotency via renewal_checkpoints, keyed by (vpsid,
created_epoch, period_index)). This module is a SEPARATE, NEWER concept: what should happen
when a VPS runs out of bandwidth BEFORE its next scheduled renewal is due? Before this
module existed, the answer was simply "nothing automatic happens" -- that remains the exact
default behavior (`ExhaustedBandwidthMode.DISABLED`) until an operator explicitly opts in.

SAFETY, EXPLICIT: this module NEVER calls the Virtualizor API and NEVER itself changes a real
customer quota. It only decides POLICY (is an emergency grant eligible right now, is a VPS
already queued, is a cooldown/per-period cap in effect) and persists that decision durably
and auditably. Actually applying an eligible, proposed emergency grant to a live quota must
go through the SAME writer.py apply path the existing renewal engine uses (WRITE_APPROVED-
gated, dry-run-previewable, idempotent) -- that execution wiring is deliberately NOT done
here; see docs/DEVELOPMENT_PROGRESS.md for the exact next step. This keeps "detect and decide
policy" (this module, fully implemented and tested) cleanly separated from "actually write to
production" (not touched by this change at all).

TWO MODES, mutually exclusive, administrator-selected (never both active at once):
  - EMERGENCY_GRANT: propose a small, configurable top-up (default 1 GB) when a VPS is
    exhausted with no grant currently due, subject to a cooldown and a maximum number of
    grants per calendar month (both configurable). Exceeding either limit does NOT silently
    grant anyway -- it is reported as blocked, letting the operator decide (e.g. by raising
    the cap or switching that VPS to manual review).
  - MANUAL_REVIEW: never auto-grant; record the exhaustion in a durable, auditable queue for
    a human administrator to acknowledge and resolve, with full history retained.

IDEMPOTENCY: each table enforces "at most one PENDING-ACTION entry per vpsid at a time" --
calling evaluate_exhaustion() repeatedly for a VPS that is still in the same exhaustion
episode never creates a duplicate entry while one is still PROPOSED (grants) or un-resolved
(reviews); it returns the status of the existing one instead. Once a grant is terminally
APPLIED or EXPIRED, a later exhaustion for the same vpsid is evaluated fresh against the
cooldown/max-per-period history (never permanently stuck reporting "already proposed"); once
a review is RESOLVED, a later exhaustion opens a genuinely new review entry.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = "emergency_policy.sqlite3"

MODE_DISABLED = "disabled"
MODE_EMERGENCY_GRANT = "emergency_grant"
MODE_MANUAL_REVIEW = "manual_review"
VALID_MODES = (MODE_DISABLED, MODE_EMERGENCY_GRANT, MODE_MANUAL_REVIEW)

BUILTIN_DEFAULT_EMERGENCY_GRANT_GB = Decimal("1")
BUILTIN_DEFAULT_COOLDOWN_HOURS = 24 * 7  # one week -- conservative default, operator-configurable
BUILTIN_DEFAULT_MAX_GRANTS_PER_MONTH = 1

STATUS_PROPOSED = "PROPOSED"
STATUS_APPLIED = "APPLIED"
STATUS_EXPIRED = "EXPIRED"

REVIEW_PENDING = "PENDING"
REVIEW_ACKNOWLEDGED = "ACKNOWLEDGED"
REVIEW_RESOLVED = "RESOLVED"

RESULT_NOT_APPLICABLE_UNLIMITED = "NOT_APPLICABLE_UNLIMITED"
RESULT_NOT_EXHAUSTED = "NOT_EXHAUSTED"
RESULT_DISABLED = "DISABLED"
RESULT_ALREADY_QUEUED = "ALREADY_QUEUED"
RESULT_ALREADY_PROPOSED = "ALREADY_PROPOSED"
RESULT_GRANT_PROPOSED = "GRANT_PROPOSED"
RESULT_GRANT_BLOCKED_COOLDOWN = "GRANT_BLOCKED_COOLDOWN"
RESULT_GRANT_BLOCKED_MAX_PER_MONTH = "GRANT_BLOCKED_MAX_PER_MONTH"
RESULT_QUEUED_FOR_REVIEW = "QUEUED_FOR_REVIEW"

SCHEMA = """
CREATE TABLE IF NOT EXISTS emergency_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vpsid INTEGER NOT NULL,
    amount_gb TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    applied_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_emergency_grants_vpsid ON emergency_grants(vpsid);

CREATE TABLE IF NOT EXISTS manual_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vpsid INTEGER NOT NULL,
    reason TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    resolved_at TEXT,
    resolution_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_manual_review_vpsid ON manual_review_queue(vpsid);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


@dataclass(frozen=True)
class ExhaustedBandwidthPolicy:
    """Resolved policy for ONE VPS's exhausted-bandwidth handling -- deliberately separate
    from policy.Policy (the regular monthly-grant policy) since this is an independent,
    newer, optional axis. `mode` defaults to DISABLED everywhere it's constructed without an
    explicit override, so existing installations/tests get IDENTICAL behavior to before this
    module existed unless an operator positively opts in."""
    mode: str = MODE_DISABLED
    emergency_grant_amount_gb: Decimal = BUILTIN_DEFAULT_EMERGENCY_GRANT_GB
    cooldown_hours: int = BUILTIN_DEFAULT_COOLDOWN_HOURS
    max_grants_per_month: int = BUILTIN_DEFAULT_MAX_GRANTS_PER_MONTH

    def __post_init__(self):
        if self.mode not in VALID_MODES:
            raise ValueError(f"invalid exhausted-bandwidth mode: {self.mode!r} (must be one of {VALID_MODES})")
        if self.emergency_grant_amount_gb < 0:
            raise ValueError("emergency_grant_amount_gb must not be negative")
        if self.cooldown_hours < 0:
            raise ValueError("cooldown_hours must not be negative")
        if self.max_grants_per_month < 1:
            raise ValueError("max_grants_per_month must be at least 1")


def _get_open_grant(conn: sqlite3.Connection, vpsid: int):
    """"Open" means PENDING ACTION, i.e. still PROPOSED -- an idempotency guard against
    creating a second row while the first is still awaiting the (separate, not-yet-wired)
    apply step. APPLIED and EXPIRED are both terminal/closed: an APPLIED grant must NOT
    block a later, genuinely new exhaustion episode from being evaluated against cooldown/
    max-per-month (that history is exactly what _last_grant_at/_grants_in_current_month
    check next) -- it must never get permanently stuck reporting "already proposed"."""
    return conn.execute(
        "SELECT id, amount_gb, detected_at, status FROM emergency_grants "
        "WHERE vpsid=? AND status = ? ORDER BY id DESC LIMIT 1",
        (vpsid, STATUS_PROPOSED),
    ).fetchone()


def _get_open_review(conn: sqlite3.Connection, vpsid: int):
    return conn.execute(
        "SELECT id, reason, detected_at, status FROM manual_review_queue "
        "WHERE vpsid=? AND status != ? ORDER BY id DESC LIMIT 1",
        (vpsid, REVIEW_RESOLVED),
    ).fetchone()


def _grants_in_current_month(conn: sqlite3.Connection, vpsid: int, now: datetime) -> int:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM emergency_grants WHERE vpsid=? AND detected_at >= ? "
        "AND status IN (?, ?)",
        (vpsid, month_start, STATUS_PROPOSED, STATUS_APPLIED),
    ).fetchone()
    return row[0]


def _last_grant_at(conn: sqlite3.Connection, vpsid: int):
    row = conn.execute(
        "SELECT detected_at FROM emergency_grants WHERE vpsid=? AND status IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (vpsid, STATUS_PROPOSED, STATUS_APPLIED),
    ).fetchone()
    return row[0] if row else None


def evaluate_exhaustion(conn: sqlite3.Connection, vpsid: int, quota_gb, used_gb,
                         policy: ExhaustedBandwidthPolicy, now: datetime = None,
                         reason: str = None) -> dict:
    """The one entry point this module exposes for the detection/reporting layer (CLI/Web/
    Telegram will all call this same function -- never three independent reimplementations).
    Never raises on a quota<=0 (unlimited) VPS -- unlimited is never "exhausted", matching
    the same rule analytics.py/policy.py already use elsewhere in this project. Returns a
    dict with at least {"result": ..., "vpsid": vpsid} plus result-specific detail fields.
    Idempotent: calling this repeatedly while the SAME exhaustion persists never creates a
    second open record."""
    now = now or datetime.now(timezone.utc)
    try:
        quota = Decimal(str(quota_gb))
        used = Decimal(str(used_gb))
    except Exception:
        return {"result": RESULT_NOT_EXHAUSTED, "vpsid": vpsid,
                "note": "quota/used not parseable -- never guessed as exhausted"}

    if quota <= 0:
        return {"result": RESULT_NOT_APPLICABLE_UNLIMITED, "vpsid": vpsid}
    if used < quota:
        return {"result": RESULT_NOT_EXHAUSTED, "vpsid": vpsid}

    # Exhausted (used >= quota) from here on.
    if policy.mode == MODE_DISABLED:
        return {"result": RESULT_DISABLED, "vpsid": vpsid,
                "note": "exhausted-bandwidth policy is disabled -- no automatic action, "
                        "identical to pre-v1.8.0 behavior"}

    if policy.mode == MODE_MANUAL_REVIEW:
        existing = _get_open_review(conn, vpsid)
        if existing is not None:
            return {"result": RESULT_ALREADY_QUEUED, "vpsid": vpsid, "review_id": existing[0],
                    "status": existing[3]}
        detected_at = now.isoformat()
        final_reason = reason or f"bandwidth exhausted (used {used} GB >= quota {quota} GB)"
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT INTO manual_review_queue (vpsid, reason, detected_at, status) "
                "VALUES (?,?,?,?)",
                (vpsid, final_reason, detected_at, REVIEW_PENDING),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"result": RESULT_QUEUED_FOR_REVIEW, "vpsid": vpsid, "review_id": cur.lastrowid,
                "detected_at": detected_at, "reason": final_reason}

    # MODE_EMERGENCY_GRANT
    existing = _get_open_grant(conn, vpsid)
    if existing is not None:
        return {"result": RESULT_ALREADY_PROPOSED, "vpsid": vpsid, "grant_id": existing[0],
                "status": existing[3]}

    last_grant_at = _last_grant_at(conn, vpsid)
    if last_grant_at is not None:
        try:
            elapsed_hours = (now - datetime.fromisoformat(last_grant_at)).total_seconds() / 3600
        except Exception:
            elapsed_hours = None
        if elapsed_hours is not None and elapsed_hours < policy.cooldown_hours:
            return {"result": RESULT_GRANT_BLOCKED_COOLDOWN, "vpsid": vpsid,
                    "last_grant_at": last_grant_at,
                    "cooldown_hours": policy.cooldown_hours,
                    "hours_remaining": round(policy.cooldown_hours - elapsed_hours, 2)}

    count_this_month = _grants_in_current_month(conn, vpsid, now)
    if count_this_month >= policy.max_grants_per_month:
        return {"result": RESULT_GRANT_BLOCKED_MAX_PER_MONTH, "vpsid": vpsid,
                "max_grants_per_month": policy.max_grants_per_month,
                "grants_this_month": count_this_month}

    detected_at = now.isoformat()
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO emergency_grants (vpsid, amount_gb, detected_at, status, note) "
            "VALUES (?,?,?,?,?)",
            (vpsid, str(policy.emergency_grant_amount_gb), detected_at, STATUS_PROPOSED,
             reason or f"bandwidth exhausted (used {used} GB >= quota {quota} GB)"),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"result": RESULT_GRANT_PROPOSED, "vpsid": vpsid, "grant_id": cur.lastrowid,
            "amount_gb": policy.emergency_grant_amount_gb, "detected_at": detected_at}


def mark_grant_applied(conn: sqlite3.Connection, grant_id: int, applied_at: str = None) -> None:
    """Called ONLY by the (not-yet-wired) actual-apply step once writer.py's own idempotent,
    WRITE_APPROVED-gated apply path has genuinely applied this grant to the live quota --
    never called speculatively. Marking PROPOSED->APPLIED here is pure bookkeeping; it does
    not itself write anything to Virtualizor."""
    conn.execute("UPDATE emergency_grants SET status=?, applied_at=? WHERE id=? AND status=?",
                 (STATUS_APPLIED, applied_at or utcnow_iso(), grant_id, STATUS_PROPOSED))


def expire_stale_grant(conn: sqlite3.Connection, grant_id: int) -> None:
    """An operator/automation decided a PROPOSED grant is no longer relevant (e.g. the VPS
    renewed normally in the meantime) -- marks it EXPIRED so evaluate_exhaustion() can open a
    fresh episode for this vpsid if it becomes exhausted again. Never deletes the row (audit
    history preserved)."""
    conn.execute("UPDATE emergency_grants SET status=? WHERE id=? AND status=?",
                 (STATUS_EXPIRED, grant_id, STATUS_PROPOSED))


def acknowledge_review(conn: sqlite3.Connection, review_id: int, acknowledged_by: str,
                        acknowledged_at: str = None) -> None:
    conn.execute(
        "UPDATE manual_review_queue SET status=?, acknowledged_at=?, acknowledged_by=? "
        "WHERE id=? AND status=?",
        (REVIEW_ACKNOWLEDGED, acknowledged_at or utcnow_iso(), acknowledged_by, review_id, REVIEW_PENDING),
    )


def resolve_review(conn: sqlite3.Connection, review_id: int, resolution_notes: str,
                    resolved_at: str = None) -> None:
    """Resolvable from PENDING or ACKNOWLEDGED -- an operator may resolve directly without a
    separate acknowledge step. Once RESOLVED, a future exhaustion for the same vpsid opens a
    genuinely new episode (see _get_open_review's `status != RESOLVED` filter)."""
    conn.execute(
        "UPDATE manual_review_queue SET status=?, resolved_at=?, resolution_notes=? "
        "WHERE id=? AND status IN (?, ?)",
        (REVIEW_RESOLVED, resolved_at or utcnow_iso(), resolution_notes, review_id,
         REVIEW_PENDING, REVIEW_ACKNOWLEDGED),
    )


def list_pending_reviews(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id, vpsid, reason, detected_at, status, acknowledged_at, acknowledged_by "
        "FROM manual_review_queue WHERE status != ? ORDER BY detected_at DESC LIMIT ?",
        (REVIEW_RESOLVED, limit),
    ).fetchall()
    return [{"id": r[0], "vpsid": r[1], "reason": r[2], "detected_at": r[3], "status": r[4],
             "acknowledged_at": r[5], "acknowledged_by": r[6]} for r in rows]


def list_proposed_grants(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id, vpsid, amount_gb, detected_at, status, note FROM emergency_grants "
        "WHERE status=? ORDER BY detected_at DESC LIMIT ?",
        (STATUS_PROPOSED, limit),
    ).fetchall()
    return [{"id": r[0], "vpsid": r[1], "amount_gb": r[2], "detected_at": r[3],
             "status": r[4], "note": r[5]} for r in rows]


# ---------- settings persistence (mirrors hostnet_sync.py's load_settings/save_* pattern) ----

def _settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "emergency-policy-settings.json")


def load_policy_settings(config_dir: str) -> ExhaustedBandwidthPolicy:
    """Defaults to MODE_DISABLED (identical to pre-v1.8.0 behavior) if the settings file is
    absent, unreadable, or malformed -- never raises into a caller that just wants to know
    the current policy, and never silently resolves a parse failure into an ACTIVE mode."""
    path = _settings_path(config_dir)
    if not os.path.exists(path):
        return ExhaustedBandwidthPolicy()
    try:
        data = json.loads(Path(path).read_text())
        return ExhaustedBandwidthPolicy(
            mode=data.get("mode", MODE_DISABLED),
            emergency_grant_amount_gb=Decimal(str(data.get("emergency_grant_amount_gb",
                                                             BUILTIN_DEFAULT_EMERGENCY_GRANT_GB))),
            cooldown_hours=int(data.get("cooldown_hours", BUILTIN_DEFAULT_COOLDOWN_HOURS)),
            max_grants_per_month=int(data.get("max_grants_per_month",
                                               BUILTIN_DEFAULT_MAX_GRANTS_PER_MONTH)),
        )
    except (OSError, ValueError, TypeError):
        return ExhaustedBandwidthPolicy()


def save_policy_settings(config_dir: str, policy: ExhaustedBandwidthPolicy) -> None:
    """Atomic write (tmp + os.replace), same crash-safety convention used by every other
    settings writer in this project (see hostnet_sync.save_monitored_interface). `policy`
    having already passed __post_init__ validation is this function's only safety check --
    it never re-validates or silently coerces an invalid value."""
    path = _settings_path(config_dir)
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    data = {
        "mode": policy.mode,
        "emergency_grant_amount_gb": str(policy.emergency_grant_amount_gb),
        "cooldown_hours": policy.cooldown_hours,
        "max_grants_per_month": policy.max_grants_per_month,
    }
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
