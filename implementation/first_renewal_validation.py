"""first_renewal_validation.py (v1.10.11) -- automated validation of the FIRST real
production renewal write that happens after an operator explicitly authorizes automatic
renewal via `enable-auto-renewal`.

Why this exists: the operator's own stated position is correct -- `auto_renewal_enabled`
being False is a deliberate safety choice, not a technical readiness gap, and this project
should never fabricate or wait around for a real billing event just to "prove" something.
What it CAN do, honestly, is make the eventual real event self-documenting: the moment
`enable_auto_renewal()` runs, this module arms a durable per-node marker. Whenever
`cmd_apply_due`/`cmd_apply` next actually attempts a real write for that node and gets a
result back from `writer.apply_renewal()`, this module -- and ONLY this module -- consumes
that one-time marker and records a clear, permanent, human-readable record of exactly what
was expected vs. what actually happened, then (if a push channel is configured) sends an
alert, success or failure.

SAFETY, EXPLICIT:
  - This module NEVER calls the Virtualizor API, NEVER decides whether to apply a renewal,
    and NEVER retries, blocks, or alters a write in any way. It only OBSERVES a result that
    writer.apply_renewal()/the existing exactly-once machinery already independently
    produced on its own. Removing this module entirely would not change what gets written
    to Virtualizor, only how the FIRST outcome gets recorded and reported.
  - "Freeze on inconsistency" is NOT reimplemented here -- it already exists, unconditionally,
    for EVERY write (not just the first), via writer.py's UNKNOWN_NEEDS_VERIFICATION status:
    any mismatch between expected and post-write-verified quota already blocks every further
    automatic attempt for that exact (node, vpsid, period) until an operator resolves it
    (`resolve-operation`) -- see test_writer.py's `test_duplicate_run_same_period_blocked_
    after_unknown`. This module's only job is to make that outcome -- APPLIED or
    UNKNOWN_NEEDS_VERIFICATION -- impossible to miss the first time it happens.
  - The durable outcome file is append-only (never overwritten in place) and is written via
    the same tmp-file-then-os.replace pattern used everywhere else in this project for
    crash-safety.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _dir(config_dir: str) -> str:
    return os.path.join(config_dir, "first-renewal-validation")


def _pending_marker_path(config_dir: str, node_id: str) -> str:
    return os.path.join(_dir(config_dir), f"{node_id}.pending.json")


def _outcomes_path(config_dir: str, node_id: str) -> str:
    return os.path.join(_dir(config_dir), f"{node_id}.outcomes.json")


def arm(config_dir: str, node_id: str) -> None:
    """Called by installer.enable_auto_renewal() exactly once per genuinely NEW
    authorization (never re-armed by a no-op re-run against an already-approved node) --
    marks that the NEXT real write-attempt result for this node must be specially
    recorded. Idempotent: arming an already-armed node just refreshes the timestamp."""
    path = _pending_marker_path(config_dir, node_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(json.dumps(
        {"armed_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")


def is_armed(config_dir: str, node_id: str) -> bool:
    return os.path.exists(_pending_marker_path(config_dir, node_id))


def disarm(config_dir: str, node_id: str) -> None:
    path = _pending_marker_path(config_dir, node_id)
    if os.path.exists(path):
        os.remove(path)


def record_first_outcome(config_dir: str, node_id: str, vpsid: int, period_key: str,
                          expected_quota_gb, result: dict) -> dict | None:
    """Consumes the armed marker for this node (no-op, returns None, if not armed -- e.g.
    this is the SECOND or later real write since authorization) and appends a durable
    outcome record. `result` is whatever writer.apply_renewal()/cmd_apply_due already
    independently decided -- this function changes NOTHING about it, only observes and
    records it.

    `verified_ok` is True only for a genuinely clean APPLIED result whose post-write
    live-verified quota exactly matches what was expected -- the SAME comparison
    writer.apply_renewal() itself already performed to decide APPLIED vs.
    UNKNOWN_NEEDS_VERIFICATION in the first place; this is not a second, independent
    judgment, just making that one highly visible for the first real occurrence."""
    if not is_armed(config_dir, node_id):
        return None
    disarm(config_dir, node_id)

    actual = result.get("verified_after_quota_gb")
    verified_ok = (result.get("status") == "APPLIED" and actual is not None
                   and str(actual) == str(expected_quota_gb))
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "node_id": node_id,
        "vpsid": vpsid,
        "period_key": period_key,
        "expected_quota_gb": str(expected_quota_gb),
        "actual_quota_gb": str(actual) if actual is not None else None,
        "write_status": result.get("status"),
        "operation_id": result.get("operation_id"),
        "verified_ok": verified_ok,
    }

    path = _outcomes_path(config_dir, node_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = load_outcomes(config_dir, node_id)
    existing.append(record)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(existing, indent=2) + "\n")
    os.replace(tmp, path)
    return record


def load_outcomes(config_dir: str, node_id: str) -> list:
    path = _outcomes_path(config_dir, node_id)
    if not os.path.exists(path):
        return []
    try:
        return json.loads(Path(path).read_text())
    except (ValueError, OSError):
        return []  # fail safe: a corrupt/unreadable file means "no prior outcomes", never crash


def build_notification_text(record: dict) -> str:
    if record["verified_ok"]:
        return (
            f"FIRST LIVE RENEWAL VALIDATED (node={record['node_id']} vpsid={record['vpsid']}): "
            f"expected {record['expected_quota_gb']} GB, live-verified actual "
            f"{record['actual_quota_gb']} GB -- MATCH. operation_id={record['operation_id']}."
        )
    return (
        f"FIRST LIVE RENEWAL NEEDS REVIEW (node={record['node_id']} vpsid={record['vpsid']}): "
        f"status={record['write_status']}, expected {record['expected_quota_gb']} GB, "
        f"actual {record['actual_quota_gb']}. This specific (node, vpsid, period) is ALREADY "
        f"blocked from any further automatic attempt until you run `resolve-operation` -- "
        f"this message is reporting that existing safety outcome, not asking you to act "
        f"faster than it already protects you. operation_id={record['operation_id']}."
    )
