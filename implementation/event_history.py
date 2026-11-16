"""Durable, auditable event history derived from inventory snapshot diffs (inventory.py's
`_diff_snapshots` output). Stored in inventory.sqlite3's `inventory_events` table -- still
entirely separate from shadow_monitor.py's renewal ledger.

SAFETY BOUNDARY (the whole reason this module exists as it does): an administrator may
change a VPS's quota or USED at any time, including right around a real monthly reset. This
module NEVER treats a quota change or a USED decrease as proof of a renewal-eligible reset --
that determination is, and remains, the EXCLUSIVE job of engine.py's/shadow_monitor.py's own
anniversary-validated reset detection, which this module only ever CROSS-REFERENCES (never
reimplements or overrides). Every event here carries an explicit confidence level:

  CONFIRMED -- an observable FACT about what changed (a field value differed between two
               complete snapshots). This does NOT mean the underlying CAUSE is confirmed --
               e.g. a CONFIRMED quota change event records that quota changed, never why.
  POSSIBLE  -- a plausible interpretation (e.g. "this used_gb drop could be a reset") that
               lacks corroborating evidence from the renewal ledger.
  UNKNOWN   -- detected but with no basis for even a POSSIBLE interpretation.

This module never calls into writer.py, never touches WRITE_APPROVED, and never feeds back
into renewal eligibility -- it is a pure, read-only, observational audit trail.
"""
from __future__ import annotations

from decimal import Decimal

CONFIRMED = "CONFIRMED"
POSSIBLE = "POSSIBLE"
UNKNOWN = "UNKNOWN"

EVENT_VPS_CREATED = "VPS_CREATED"
EVENT_VPS_DELETED = "VPS_DELETED"
EVENT_VPS_ID_REUSE_SUSPECTED = "VPS_ID_REUSE_SUSPECTED"
EVENT_VPS_MIGRATION = "VPS_MIGRATION"
EVENT_IP_REASSIGNMENT = "IP_REASSIGNMENT"
EVENT_HOSTNAME_CHANGE = "HOSTNAME_CHANGE"
EVENT_RESOURCE_CHANGE = "RESOURCE_CHANGE"
EVENT_LOCK_ENABLED = "LOCK_ENABLED"
EVENT_LOCK_DISABLED = "LOCK_DISABLED"
EVENT_QUOTA_INCREASE = "QUOTA_INCREASE"
EVENT_QUOTA_DECREASE = "QUOTA_DECREASE"
EVENT_LIMITED_TO_UNLIMITED = "LIMITED_TO_UNLIMITED"
EVENT_UNLIMITED_TO_LIMITED = "UNLIMITED_TO_LIMITED"
EVENT_USED_INCREASE_OBSERVED = "USED_INCREASE_OBSERVED"
EVENT_POSSIBLE_RESET = "POSSIBLE_RESET"
EVENT_CONFIRMED_RESET = "CONFIRMED_RESET"


def _is_unlimited(quota) -> bool:
    try:
        return Decimal(str(quota)) <= 0
    except Exception:
        return False


def _find_corroborating_renewal(ledger_conn, node_id: str, vpsid: int):
    """Looks for a CONFIRMED APPLIED operation in the EXISTING renewal ledger (via a
    separate connection the caller owns) -- never writes to it, never a second renewal
    ledger. Returns the operation_id if found, else None. `ledger_conn` is optional
    specifically so this module can run standalone wherever the ledger isn't available
    (its absence only ever DOWNGRADES confidence to POSSIBLE, never fabricates CONFIRMED)."""
    if ledger_conn is None:
        return None
    try:
        row = ledger_conn.execute(
            "SELECT operation_id FROM operations WHERE node_id=? AND vpsid=? AND status='APPLIED' "
            "ORDER BY updated_at DESC LIMIT 1", (node_id, vpsid)
        ).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def derive_events(diff: dict, known_vps_lookup, node_id: str, ledger_conn=None) -> list[dict]:
    """Pure-ish: `known_vps_lookup(vpsid) -> dict|None` is injectable for tests. Never
    writes anything -- callers persist the result via persist_events()."""
    events = []

    for vpsid in diff.get("added", []):
        prior = known_vps_lookup(vpsid)
        if prior is None:
            events.append({"vpsid": vpsid, "event_type": EVENT_VPS_CREATED, "confidence": CONFIRMED,
                           "old_value": None, "new_value": None,
                           "evidence": "first appearance of this vpsid in any complete snapshot"})
        else:
            events.append({"vpsid": vpsid, "event_type": EVENT_VPS_ID_REUSE_SUSPECTED, "confidence": POSSIBLE,
                           "old_value": None, "new_value": None,
                           "evidence": f"vpsid previously known (first_seen_at={prior.get('first_seen_at')}) "
                                       f"then absent from a complete snapshot, now reappeared"})

    for vpsid in diff.get("removed", []):
        events.append({"vpsid": vpsid, "event_type": EVENT_VPS_DELETED, "confidence": CONFIRMED,
                       "old_value": None, "new_value": None,
                       "evidence": "absent from a subsequent COMPLETE snapshot"})

    for mod in diff.get("modified", []):
        vpsid = mod["vpsid"]
        for field, change in mod["changes"].items():
            old, new = change["old"], change["new"]
            if field == "node_serid":
                events.append({"vpsid": vpsid, "event_type": EVENT_VPS_MIGRATION, "confidence": CONFIRMED,
                               "old_value": old, "new_value": new, "evidence": "node_serid (node identity) changed"})
            elif field == "ip_primary":
                events.append({"vpsid": vpsid, "event_type": EVENT_IP_REASSIGNMENT, "confidence": CONFIRMED,
                               "old_value": old, "new_value": new, "evidence": "primary IP changed"})
            elif field == "hostname":
                events.append({"vpsid": vpsid, "event_type": EVENT_HOSTNAME_CHANGE, "confidence": CONFIRMED,
                               "old_value": old, "new_value": new, "evidence": "hostname changed"})
            elif field in ("ram_mb", "cores", "disk_gb"):
                events.append({"vpsid": vpsid, "event_type": EVENT_RESOURCE_CHANGE, "confidence": CONFIRMED,
                               "old_value": f"{field}={old}", "new_value": f"{field}={new}",
                               "evidence": f"{field} changed"})
            elif field == "locked":
                etype = EVENT_LOCK_ENABLED if str(new).lower() == "true" else EVENT_LOCK_DISABLED
                events.append({"vpsid": vpsid, "event_type": etype, "confidence": CONFIRMED,
                               "old_value": old, "new_value": new, "evidence": "locked flag changed"})
            elif field == "quota_gb":
                old_unlimited, new_unlimited = _is_unlimited(old), _is_unlimited(new)
                if old_unlimited != new_unlimited:
                    etype = EVENT_LIMITED_TO_UNLIMITED if new_unlimited else EVENT_UNLIMITED_TO_LIMITED
                else:
                    try:
                        etype = (EVENT_QUOTA_INCREASE if Decimal(str(new)) > Decimal(str(old))
                                 else EVENT_QUOTA_DECREASE)
                    except Exception:
                        etype = EVENT_QUOTA_INCREASE
                events.append({"vpsid": vpsid, "event_type": etype, "confidence": CONFIRMED,
                               "old_value": old, "new_value": new,
                               "evidence": "quota value changed -- NOT evidence of a renewal-eligible reset; "
                                           "see the renewal ledger's own anniversary-validated detection for that"})

    for trend in diff.get("usage_trend", []):
        vpsid = trend["vpsid"]
        try:
            old_used, new_used = Decimal(str(trend["old_used_gb"])), Decimal(str(trend["new_used_gb"]))
        except Exception:
            continue
        if new_used < old_used:
            corroborating_op = _find_corroborating_renewal(ledger_conn, node_id, vpsid)
            if corroborating_op:
                events.append({"vpsid": vpsid, "event_type": EVENT_CONFIRMED_RESET, "confidence": CONFIRMED,
                               "old_value": str(old_used), "new_value": str(new_used),
                               "evidence": f"used_gb decreased, corroborated by renewal ledger "
                                           f"operation {corroborating_op}"})
            else:
                events.append({"vpsid": vpsid, "event_type": EVENT_POSSIBLE_RESET, "confidence": POSSIBLE,
                               "old_value": str(old_used), "new_value": str(new_used),
                               "evidence": "used_gb decreased with no corroborating CONFIRMED renewal-ledger "
                                           "operation -- could be a genuine reset, a manual admin quota/usage "
                                           "adjustment, or a counter artifact. NOT sufficient proof of a "
                                           "renewal-eligible reset on its own."})
        elif new_used > old_used:
            events.append({"vpsid": vpsid, "event_type": EVENT_USED_INCREASE_OBSERVED, "confidence": CONFIRMED,
                           "old_value": str(old_used), "new_value": str(new_used),
                           "evidence": "routine bandwidth usage increase"})
    return events


def persist_events(conn, node_id: str, events: list[dict], detected_at: str) -> None:
    for e in events:
        conn.execute(
            "INSERT INTO inventory_events (node_id, vpsid, event_type, confidence, detected_at, "
            "old_value, new_value, evidence) VALUES (?,?,?,?,?,?,?,?)",
            (node_id, e["vpsid"], e["event_type"], e["confidence"], detected_at,
             e.get("old_value"), e.get("new_value"), e.get("evidence")),
        )


def query_events(conn, node_id: str, vpsid: int = None, limit: int = 100) -> list[dict]:
    if vpsid is not None:
        rows = conn.execute(
            "SELECT vpsid, event_type, confidence, detected_at, old_value, new_value, evidence "
            "FROM inventory_events WHERE node_id=? AND vpsid=? ORDER BY id DESC LIMIT ?",
            (node_id, vpsid, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT vpsid, event_type, confidence, detected_at, old_value, new_value, evidence "
            "FROM inventory_events WHERE node_id=? ORDER BY id DESC LIMIT ?",
            (node_id, limit)).fetchall()
    return [{"vpsid": r[0], "event_type": r[1], "confidence": r[2], "detected_at": r[3],
             "old_value": r[4], "new_value": r[5], "evidence": r[6]} for r in rows]
