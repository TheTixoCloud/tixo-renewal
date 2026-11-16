"""Bandwidth analytics computed from a VPS inventory snapshot (list of record dicts as
produced by virtualizor_info.parse_listvs_response / inventory.latest_records).

Core rule, preserved from the renewal engine itself: a quota <= 0 means UNLIMITED, never "zero
allowance" -- unlimited VPSs are excluded from limited-fleet sums/percentages (mixing them in
would silently misrepresent both). Missing data (a field reported as "unavailable" upstream)
is excluded from sums/counts for that metric, never coerced to zero -- a statistics module
that treats absence as zero would quietly understate real usage.
"""
from __future__ import annotations

from decimal import Decimal

UNAVAILABLE = "unavailable"


def _is_unlimited(quota) -> bool:
    try:
        return Decimal(str(quota)) <= 0
    except Exception:
        return False  # unparseable quota is treated as "unknown", not unlimited -- see below


def _is_known_decimal(value):
    if value is None or value == UNAVAILABLE:
        return False
    try:
        Decimal(str(value))
        return True
    except Exception:
        return False


def compute_bandwidth_analytics(records: list[dict], usage_threshold_percent: Decimal = None) -> dict:
    total = len(records)
    limited, unlimited, unknown_quota = [], [], []
    for r in records:
        quota = r.get("quota_gb")
        if not _is_known_decimal(quota):
            unknown_quota.append(r)
        elif _is_unlimited(quota):
            unlimited.append(r)
        else:
            limited.append(r)

    running = sum(1 for r in records if str(r.get("status")).lower() == "running")
    stopped = sum(1 for r in records if str(r.get("status")).lower() in ("stopped", "suspended"))

    limited_quota_sum = sum((Decimal(str(r["quota_gb"])) for r in limited), Decimal(0))
    limited_used = [r for r in limited if _is_known_decimal(r.get("used_gb"))]
    limited_used_sum = sum((Decimal(str(r["used_gb"])) for r in limited_used), Decimal(0))
    limited_remaining_sum = sum(
        (Decimal(str(r["quota_gb"])) - Decimal(str(r["used_gb"])) for r in limited_used), Decimal(0)
    )
    unlimited_used = [r for r in unlimited if _is_known_decimal(r.get("used_gb"))]
    unlimited_used_sum = sum((Decimal(str(r["used_gb"])) for r in unlimited_used), Decimal(0))

    over_threshold = []
    if usage_threshold_percent is not None:
        for r in limited_used:
            quota = Decimal(str(r["quota_gb"]))
            used = Decimal(str(r["used_gb"]))
            if quota > 0 and (used / quota) * 100 >= usage_threshold_percent:
                over_threshold.append(r["vpsid"])

    top_consumers = sorted(
        (r for r in records if _is_known_decimal(r.get("used_gb"))),
        key=lambda r: Decimal(str(r["used_gb"])), reverse=True,
    )[:10]

    return {
        "total_vps": total,
        "limited_vps_count": len(limited),
        "unlimited_vps_count": len(unlimited),
        "unknown_quota_vps_count": len(unknown_quota),
        "running_count": running,
        "stopped_count": stopped,
        "limited_quota_sum_gb": limited_quota_sum,
        "limited_used_sum_gb": limited_used_sum,
        "limited_used_sum_coverage": f"{len(limited_used)}/{len(limited)}",
        "limited_remaining_sum_gb": limited_remaining_sum,
        "unlimited_used_sum_gb": unlimited_used_sum if unlimited_used else UNAVAILABLE,
        "unlimited_used_sum_coverage": f"{len(unlimited_used)}/{len(unlimited)}",
        "vps_over_threshold": over_threshold,
        "top_consumers": [{"vpsid": r["vpsid"], "used_gb": str(r["used_gb"])} for r in top_consumers],
    }


def compute_renewal_outcome_counts(conn, node_id: str) -> dict:
    """Pulls renewal status counts straight from the EXISTING renewal ledger (via writer.py's
    own schema) -- analytics never maintains its own copy of renewal outcomes, just reads the
    one source of truth."""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM operations WHERE node_id=? GROUP BY status", (node_id,)
    ).fetchall()
    counts = {status: count for status, count in rows}
    return {
        "applied": counts.get("APPLIED", 0),
        "deferred_locked": counts.get("DEFERRED_LOCKED", 0),
        "deferred_expired": counts.get("DEFERRED_EXPIRED_NEEDS_REVIEW", 0),
        "unknown_needs_verification": counts.get("UNKNOWN_NEEDS_VERIFICATION", 0),
        "api_reported_error": counts.get("API_REPORTED_ERROR", 0),
    }
