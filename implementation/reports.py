"""Scheduled report generation and delivery (v1.8.0 Part 4) -- built entirely on
`control_center_api.py` (never re-deriving business logic) and `telegram_transport.py` for
delivery. No new systemd timer is introduced: scheduling is checked once per iteration of
the ALREADY-RUNNING Telegram long-polling loop (see `telegram_bot.run_polling_loop`'s
`on_iteration`/report-check integration) -- piggybacking on an existing process rather than
adding a new one, matching the "preserve existing production timers, don't add new ones
casually" discipline the rest of this project already follows.

HONESTY RULES, explicit:
  - A report section with no data (e.g. network not configured, no inventory synced) says so
    plainly -- never fabricated.
  - A report is only ever considered "sent" if Telegram's API actually confirmed delivery
    (`ok: true` in the sendMessage response) for that specific chat_id -- a failed delivery
    to one admin is reported as a failure for that admin, never silently dropped nor
    reported as a blanket success for all.
  - VPS restart/reboot events are NEVER inferred from a currently-"running" status -- this
    module does not claim to report them at all (no reliable event-history source has been
    wired for that yet; omitted entirely rather than guessed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def _format_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "unavailable"
    for unit, divisor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= divisor:
            return f"{n / divisor:.2f} {unit}"
    return f"{n:.0f} B"


def _primary_ipv4(host_snapshot: dict) -> str:
    for iface in host_snapshot.get("network_interfaces", []):
        if not iface.get("is_virtual") and iface.get("ipv4") and iface["ipv4"] != "unavailable":
            return iface["ipv4"]
    return "unavailable"


def generate_report_text(config_dir: str, state_dir: str, period_label: str = "Daily",
                          now: datetime = None) -> str:
    """Builds the full report text for one period (`period_label` is purely a display
    string, e.g. "Daily"/"Weekly"/"Monthly" -- the actual data windows shown are always
    today/this-week/this-month from traffic_analytics, regardless of which schedule
    triggered the report, since those are the periods an operator cares about regardless of
    cadence)."""
    import control_center_api as api
    import jalali

    now = now or datetime.now(timezone.utc)
    snap = api.get_full_snapshot(config_dir, state_dir)
    host = snap["host"]
    net = snap["network"]
    inv_overview = snap["inventory"]
    renewal = snap["renewal"]
    ep_overview = snap["emergency_policy"]
    tz = ZoneInfo(snap["display_timezone"])
    local_now = now.astimezone(tz)

    lines = [
        f"TixoCloud Control Center -- {period_label} Report",
        f"Generated: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"({jalali.format_jalali_date(local_now)})",
        "",
        f"Hostname: {host.get('os', {}).get('hostname', 'unavailable')}",
        f"Primary IPv4: {_primary_ipv4(host)}",
    ]

    usage = host.get("usage", {})
    uptime = host.get("uptime", {})
    temp = host.get("temperature", {})
    lines.append("")
    lines.append("-- Host Health --")
    lines.append(f"CPU/RAM usage: {usage}" if usage else "CPU/RAM usage: unavailable")
    lines.append(f"Load/uptime: {uptime}" if uptime else "Load/uptime: unavailable")
    if temp.get("available"):
        lines.append(f"Temperature zones: {len(temp.get('zones', []))} reported")
    else:
        lines.append(f"Temperature: unavailable ({temp.get('reason', 'unknown')})")

    lines.append("")
    lines.append("-- Network Traffic --")
    if not net.get("configured"):
        lines.append("No network interface configured for host traffic accounting.")
    else:
        lines.append(f"Monitored interface: {net['interface']}")
        cum = net.get("cumulative")
        if cum:
            rx, tx = cum["cumulative_rx_bytes"], cum["cumulative_tx_bytes"]
            lines.append(f"Persistent recorded totals -- Download (RX): {_format_bytes(rx)}, "
                          f"Upload (TX): {_format_bytes(tx)}, Combined: {_format_bytes(rx + tx)}")
        else:
            lines.append("Persistent recorded totals: no samples yet.")
        bundle = net.get("traffic_analytics", {})
        for key, label in (("today", "Today"), ("current_week", "This week"),
                            ("current_gregorian_month", "This month (Gregorian)"),
                            ("current_jalali_month", "This month (Jalali)")):
            period = bundle.get(key)
            if not period:
                continue
            if period["coverage_status"] == "NO_DATA":
                lines.append(f"  {label}: no data yet")
            elif period["coverage_status"] == "PARTIAL_COVERAGE":
                lines.append(f"  {label}: {_format_bytes(period['combined_bytes'])} "
                              f"(partial coverage -- {period['coverage_note']})")
            else:
                lines.append(f"  {label}: {_format_bytes(period['combined_bytes'])} combined")
        live = api.get_live_network_reading(net["interface"])
        if live["ok"]:
            lines.append(f"Raw kernel counter (since last interface counter reset) -- "
                          f"RX: {_format_bytes(live['rx_bytes'])}, "
                          f"TX: {_format_bytes(live['tx_bytes'])} "
                          f"-- NOT added to persistent recorded totals above.")

    lines.append("")
    lines.append("-- Virtualizor VPS Inventory --")
    fleet = inv_overview["fleet"]
    if fleet["total_vps"] == 0:
        lines.append("No inventory synced yet.")
    else:
        lines.append(f"Total: {fleet['total_vps']}  Running: {fleet['running_count']}  "
                      f"Stopped: {fleet['stopped_count']}  Suspended: {fleet['suspended_count']}  "
                      f"Unknown: {fleet['unknown_status_count']}")
        lines.append(f"Limited-quota usage: {_format_bytes(float(fleet['limited_used_sum_gb']) * 1e9)} "
                      f"of {_format_bytes(float(fleet['limited_quota_sum_gb']) * 1e9)} "
                      f"({fleet['limited_used_sum_coverage']} VPS reporting usage)")
        sync_info = inv_overview.get("sync_info")
        if sync_info:
            lines.append(f"Last inventory sync: {sync_info.get('captured_at')}")

    lines.append("")
    lines.append("-- Renewals --")
    lines.append(f"Write approved: {renewal.get('write_approved')}  "
                  f"Auto-renewal enabled: {renewal.get('auto_renewal_enabled')}  "
                  f"Monthly grant: {renewal.get('monthly_grant_gb')} GB")
    for node_id, info in renewal.get("per_node", {}).items():
        if not info.get("initialized"):
            lines.append(f"Node {node_id}: not initialized")
            continue
        lines.append(f"Node {node_id}: {info['operations_total']} total operations, "
                      f"{info['operations_unresolved']} unresolved, "
                      f"{len(info.get('failed_or_uncertain', []))} failed/uncertain recently")

    lines.append("")
    lines.append("-- Emergency Bandwidth Policy --")
    lines.append(f"Mode: {ep_overview['mode']}")
    if ep_overview["pending_reviews"]:
        lines.append(f"Pending manual reviews: {len(ep_overview['pending_reviews'])}")
    if ep_overview["proposed_grants"]:
        lines.append(f"Proposed emergency grants: {len(ep_overview['proposed_grants'])}")
    if not ep_overview["pending_reviews"] and not ep_overview["proposed_grants"]:
        lines.append("No pending reviews or proposed grants.")

    lines.append("")
    lines.append("-- Backup --")
    lines.append("Scheduled/encrypted backup delivery is not yet implemented.")

    return "\n".join(lines)


def chunk_telegram_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list:
    """Splits `text` into chunks that each fit Telegram's message length limit, breaking on
    line boundaries where possible (never mid-word if a line fits, only falling back to a
    hard character split for a single line that alone exceeds `max_len`)."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1  # +1 for the newline that will rejoin it
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        if line_len > max_len:
            # a single line longer than the whole limit -- hard split as a last resort
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i + max_len])
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_report_to_admins(transport, admin_ids: list, report_text: str) -> dict:
    """Sends `report_text` (chunked as needed) to every admin_id. Returns {chat_id: {"ok":
    bool, "error": str|None}} -- a report is NEVER claimed as "sent" for a recipient unless
    Telegram's own API response confirms it (`ok: true`); a failure for one admin never
    blocks delivery attempts to the others."""
    import telegram_transport as tt

    chunks = chunk_telegram_message(report_text)
    results = {}
    for admin_id in admin_ids:
        delivered_all = True
        error = None
        for chunk in chunks:
            try:
                response = transport.call("sendMessage", {"chat_id": admin_id, "text": chunk})
                if not (isinstance(response, dict) and response.get("ok")):
                    delivered_all = False
                    error = f"Telegram reported ok=false: {response}"
                    break
            except tt.TelegramTransportError as e:
                delivered_all = False
                error = str(e)
                break
        results[admin_id] = {"ok": delivered_all, "error": error}
    return results


# ---------- scheduling: checked once per polling-loop iteration, no new timer --------------

def _last_report_sent_path(state_dir: str) -> str:
    return os.path.join(state_dir, "telegram-report-schedule.json")


def load_last_report_sent_date(state_dir: str) -> str:
    """Returns the local calendar date (YYYY-MM-DD, in whatever timezone was active when it
    was saved) the report was last successfully sent, or None if never."""
    path = _last_report_sent_path(state_dir)
    if not os.path.exists(path):
        return None
    try:
        return json.loads(Path(path).read_text()).get("last_sent_date")
    except (OSError, ValueError, TypeError):
        return None


def save_last_report_sent_date(state_dir: str, date_str: str) -> None:
    path = _last_report_sent_path(state_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps({"last_sent_date": date_str}))
    os.replace(tmp, path)


def should_send_scheduled_report(settings, last_sent_date: str, now: datetime = None) -> bool:
    """True exactly once per local calendar day, at or after the configured
    report_hour:report_minute in settings.report_timezone -- never twice for the same date
    (idempotent against a polling loop that ticks every second), and never retroactively
    "catches up" on skipped days beyond sending once the next time this is checked (no
    backlog of missed reports is ever queued)."""
    now = now or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(settings.report_timezone)
    except Exception:
        return False
    local_now = now.astimezone(tz)
    today_str = local_now.strftime("%Y-%m-%d")
    if last_sent_date == today_str:
        return False
    scheduled_minutes = settings.report_hour * 60 + settings.report_minute
    now_minutes = local_now.hour * 60 + local_now.minute
    return now_minutes >= scheduled_minutes
