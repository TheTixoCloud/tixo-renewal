"""Read-only reporting over the EXISTING renewal ledger (shadow_monitor.py's `identities`/
`operations` tables) -- this module is NEVER a new detection mechanism. A "confirmed reset"
here is exactly what engine.py's own anniversary-validated logic already decided and
writer.py already recorded as an APPLIED operation; this project does not claim to detect
Virtualizor's native USED reset independently of its own controlled renewal process (see
docs/native-reset-investigation.md) -- so "confirmed renewal" and "confirmed reset" are the
same event here, not two independently-confirmed facts.

Never invents history for a period before monitoring started: `monitoring_coverage_start` is
always the identity's own `first_seen_at`, and callers should treat anything before that date
as genuinely unknown, not zero.
"""
from __future__ import annotations


def get_vps_renewal_history(conn, node_id: str, vpsid: int, inventory_conn=None) -> dict:
    """`conn` is the renewal ledger connection (shadow_monitor.py's db). `inventory_conn` is
    an OPTIONAL separate connection to inventory.sqlite3 -- only used to report a migration
    count from event_history's observational events; never required, never merged."""
    rows = conn.execute(
        "SELECT status, period_key, created_at, updated_at FROM operations "
        "WHERE node_id=? AND vpsid=? ORDER BY created_at", (node_id, vpsid)
    ).fetchall()
    applied = [r for r in rows if r[0] == "APPLIED"]
    deferred = [r for r in rows if r[0] == "DEFERRED_LOCKED"]
    expired = [r for r in rows if r[0] == "DEFERRED_EXPIRED_NEEDS_REVIEW"]
    failed_or_unknown = [r for r in rows if r[0] in ("API_REPORTED_ERROR", "UNKNOWN_NEEDS_VERIFICATION")]

    identity_row = conn.execute(
        "SELECT first_seen_at FROM identities WHERE vpsid=?", (vpsid,)
    ).fetchone()
    monitoring_coverage_start = identity_row[0] if identity_row else None

    migration_count = None
    if inventory_conn is not None:
        try:
            import event_history as eh_mod
            events = eh_mod.query_events(inventory_conn, node_id, vpsid=vpsid, limit=10000)
            migration_count = sum(1 for e in events if e["event_type"] == "VPS_MIGRATION")
        except Exception:
            migration_count = None

    return {
        "vpsid": vpsid,
        "monitoring_coverage_start": monitoring_coverage_start,
        "confirmed_renewals": len(applied),
        "confirmed_resets": len(applied),  # same event in this project's controlled model
        "last_confirmed_renewal_at": applied[-1][3] if applied else None,
        "deferred_attempts": len(deferred),
        "expired_deferrals": len(expired),
        "failed_or_unknown_operations": len(failed_or_unknown),
        "total_operations": len(rows),
        "migration_count": migration_count,
    }
