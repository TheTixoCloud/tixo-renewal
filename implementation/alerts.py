"""Alerting for missed or uncertain renewals. Purely local, read-only against the shadow
monitor's own SQLite state -- never touches production, never writes anything but alert
records in this same database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import shadow_monitor as sm

# A renewal is "missed" if its expected anniversary is this many days in the past with no
# snapshot pair observed spanning it (i.e. the monitor wasn't polling, or polling stopped).
MISSED_RENEWAL_GRACE_DAYS = 5
# A FROZEN_FOR_REVIEW / plausible candidate sitting unresolved this long is worth a nag.
STALE_REVIEW_ALERT_DAYS = 7


def compute_alerts(conn, detections: list[dict], now: datetime = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    alerts = []

    anniversaries = sm.next_anniversary_report(conn)
    for row in anniversaries:
        next_ann = datetime.fromisoformat(row["next_expected_anniversary"])
        # next_anniversary_report() always returns a FUTURE date (it rolls forward past `now`),
        # so "missed" is detected differently: look for a vpsid with NO snapshot in the last
        # MISSED_RENEWAL_GRACE_DAYS even though it has a verified identity -- i.e. polling
        # itself has stalled for this vpsid, not that the date merely hasn't arrived yet.
        last_seen = conn.execute(
            "SELECT MAX(captured_at) FROM snapshots WHERE vpsid=?", (row["vpsid"],)
        ).fetchone()[0]
        if last_seen is None:
            continue
        last_seen_dt = datetime.fromisoformat(last_seen)
        gap_days = (now - last_seen_dt).total_seconds() / 86400
        if gap_days > MISSED_RENEWAL_GRACE_DAYS:
            alerts.append({
                "severity": "ALERT", "vpsid": row["vpsid"],
                "type": "POLLING_STALLED",
                "detail": f"no snapshot observed for {gap_days:.1f} days -- monitor may be down "
                          f"or this VPS may have been removed; cannot evaluate renewal eligibility",
            })

    for d in detections:
        if d["status"] in ("FROZEN_FOR_REVIEW", "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION"):
            alerts.append({
                "severity": "INFO" if d["status"] == "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION" else "WARN",
                "vpsid": d["vpsid"], "type": d["status"],
                "detail": d.get("reason", ""),
            })

    return alerts


def format_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "No alerts."
    lines = ["Alerts:"]
    for a in sorted(alerts, key=lambda x: (x["severity"] != "ALERT", x["vpsid"])):
        lines.append(f"  [{a['severity']}] vpsid={a['vpsid']} {a['type']}: {a['detail']}")
    return "\n".join(lines)
