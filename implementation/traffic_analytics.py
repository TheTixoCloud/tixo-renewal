"""Historical host-network traffic analytics (v1.8.0), built ENTIRELY on top of hostnet.py's
existing accounting primitives (get_samples_in_range / sum_deltas / get_cumulative) -- this
module adds zero new persistence, zero new schema, and never writes to hostnet's database. It
answers "how much traffic happened in period X", not "what is the live/cumulative reading"
(see hostnet.py and menu.py's live/persistent panels for that).

TIMEZONE AND CALENDAR RULES (explicit, since this is exactly the kind of thing that's easy to
get subtly wrong):
  - All stored samples are UTC ISO-8601 (unchanged, see hostnet.py). Period boundaries here
    are computed by converting "now" to the CALLER-SUPPLIED display timezone (default
    Asia/Tehran, matching menu.py's DEFAULT_DISPLAY_TZ), finding the boundary in THAT
    timezone's local wall-clock, then converting the boundary back to UTC for querying.
    This means "today" means "today in the configured timezone", not UTC-today.
  - A "week" starts on Monday (ISO-8601 convention), in the configured timezone.
  - Jalali month boundaries reuse jalali.jalali_month_start (the same logic menu.py already
    uses for display) -- never a second, independently-written Jalali boundary calculation.
  - Gregorian and Jalali "current month" periods are DIFFERENT calendars over the SAME
    underlying days except near a month boundary -- both are offered side by side, never
    assumed interchangeable.
  - Decimal (SI) byte units throughout, matching hostnet.py/menu.py's existing convention.
  - A period is never fabricated: periods that start before hostnet monitoring was ever
    activated (cumulative.first_sample_at missing or later than the period start) are
    reported with coverage_status=NO_DATA or PARTIAL_COVERAGE and an explicit note -- never
    silently treated as "zero traffic really happened".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import hostnet

DEFAULT_TIMEZONE = "Asia/Tehran"

COVERAGE_FULL = "FULL"
COVERAGE_PARTIAL = "PARTIAL_COVERAGE"
COVERAGE_NO_DATA = "NO_DATA"
COVERAGE_BEFORE_MONITORING = "BEFORE_MONITORING_START"


def _to_utc_iso(local_dt: datetime, tz: ZoneInfo) -> str:
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=tz)
    return local_dt.astimezone(timezone.utc).isoformat()


def _now_local(tz_name: str, now: datetime = None) -> datetime:
    tz = ZoneInfo(tz_name)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(tz)


def peak_combined_rate_bps(samples: list[dict]) -> float:
    """Highest single-sample instantaneous COMBINED (rx+tx) rate in bytes/sec within the
    given sample list, derived from each sample's own stored delta/gap_seconds (the 5-minute
    persistent sampler's own interval -- never a live ~1Hz reading, which this module never
    touches). Returns 0.0 if no sample has a usable (non-discontinuity, positive-gap) rate --
    never None, since "no samples" and "zero peak" are both legitimately representable as 0.0
    here (the caller's coverage_status is what distinguishes "no data" from "no traffic")."""
    peak = 0.0
    for s in samples:
        if s.get("discontinuity_type"):
            continue
        rx, tx, gap = s.get("rx_delta"), s.get("tx_delta"), s.get("gap_seconds")
        if rx is None or tx is None or not gap or gap <= 0:
            continue
        rate = (rx + tx) / gap
        if rate > peak:
            peak = rate
    return peak


def _build_period_result(conn, interface: str, label: str, start_utc_iso: str, end_utc_iso: str,
                          complete: bool, cumulative: dict = None) -> dict:
    if cumulative is None:
        cumulative = hostnet.get_cumulative(conn, interface)

    first_sample_at = cumulative.get("first_sample_at") if cumulative else None
    if first_sample_at is None:
        coverage_status = COVERAGE_NO_DATA
    elif start_utc_iso < first_sample_at:
        coverage_status = COVERAGE_PARTIAL
    else:
        coverage_status = COVERAGE_FULL

    samples = hostnet.get_samples_in_range(conn, interface, start_utc_iso, end_utc_iso)
    totals = hostnet.sum_deltas(samples)
    peak_bps = peak_combined_rate_bps(samples)

    try:
        duration_seconds = (datetime.fromisoformat(end_utc_iso) -
                            datetime.fromisoformat(start_utc_iso)).total_seconds()
    except Exception:
        duration_seconds = None
    avg_combined_bps = (totals["combined_bytes"] / duration_seconds
                        if duration_seconds and duration_seconds > 0 else None)

    if coverage_status == COVERAGE_NO_DATA:
        note = "Host network monitoring has never recorded a sample for this interface."
    elif coverage_status == COVERAGE_PARTIAL:
        note = (f"Monitoring only began at {first_sample_at} -- traffic before that point in "
                f"this period is not observable and is not included (never fabricated).")
    else:
        note = None

    return {
        "label": label,
        "interface": interface,
        "period_start": start_utc_iso,
        "period_end": end_utc_iso,
        "complete": complete,
        "rx_bytes": totals["rx_bytes"],
        "tx_bytes": totals["tx_bytes"],
        "combined_bytes": totals["combined_bytes"],
        "avg_combined_bytes_per_sec": avg_combined_bps,
        "peak_combined_bytes_per_sec": peak_bps,
        "sample_count": totals["sample_count"],
        "counted_sample_count": totals["counted_sample_count"],
        "partial_samples": totals["partial"],
        "coverage_status": coverage_status,
        "coverage_note": note,
    }


def today(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE, now: datetime = None) -> dict:
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _build_period_result(conn, interface, "today", _to_utc_iso(start_local, tz),
                                 _to_utc_iso(local_now, tz), complete=False)


def yesterday(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE, now: datetime = None) -> dict:
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    y_start = today_start - timedelta(days=1)
    return _build_period_result(conn, interface, "yesterday", _to_utc_iso(y_start, tz),
                                 _to_utc_iso(today_start, tz), complete=True)


def last_24_hours(conn, interface: str, now: datetime = None) -> dict:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    start = now_utc - timedelta(hours=24)
    return _build_period_result(conn, interface, "last_24_hours", start.isoformat(),
                                 now_utc.isoformat(), complete=False)


def current_week(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE, now: datetime = None) -> dict:
    """ISO-8601 week: Monday start, in the configured local timezone."""
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())  # Monday=0
    return _build_period_result(conn, interface, "current_week", _to_utc_iso(week_start, tz),
                                 _to_utc_iso(local_now, tz), complete=False)


def last_7_days(conn, interface: str, now: datetime = None) -> dict:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    start = now_utc - timedelta(days=7)
    return _build_period_result(conn, interface, "last_7_days", start.isoformat(),
                                 now_utc.isoformat(), complete=False)


def current_gregorian_month(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE,
                             now: datetime = None) -> dict:
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _build_period_result(conn, interface, "current_gregorian_month",
                                 _to_utc_iso(start_local, tz), _to_utc_iso(local_now, tz),
                                 complete=False)


def previous_gregorian_month(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE,
                              now: datetime = None) -> dict:
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    this_month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month_end = this_month_start  # exclusive end
    last_day_prev_month = this_month_start - timedelta(days=1)
    prev_month_start = last_day_prev_month.replace(day=1)
    return _build_period_result(conn, interface, "previous_gregorian_month",
                                 _to_utc_iso(prev_month_start, tz), _to_utc_iso(prev_month_end, tz),
                                 complete=True)


def current_jalali_month(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE,
                          now: datetime = None) -> dict:
    import jalali
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    month_start_date = jalali.jalali_month_start(local_now.date())
    start_local = datetime.combine(month_start_date, datetime.min.time(), tzinfo=tz)
    return _build_period_result(conn, interface, "current_jalali_month",
                                 _to_utc_iso(start_local, tz), _to_utc_iso(local_now, tz),
                                 complete=False)


def previous_jalali_month(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE,
                           now: datetime = None) -> dict:
    import jalali
    tz = ZoneInfo(tz_name)
    local_now = _now_local(tz_name, now)
    this_month_start_date = jalali.jalali_month_start(local_now.date())
    last_day_prev = this_month_start_date - timedelta(days=1)
    prev_month_start_date = jalali.jalali_month_start(last_day_prev)
    start_local = datetime.combine(prev_month_start_date, datetime.min.time(), tzinfo=tz)
    end_local = datetime.combine(this_month_start_date, datetime.min.time(), tzinfo=tz)
    return _build_period_result(conn, interface, "previous_jalali_month",
                                 _to_utc_iso(start_local, tz), _to_utc_iso(end_local, tz),
                                 complete=True)


def all_time(conn, interface: str, now: datetime = None) -> dict:
    """Total recorded traffic since monitoring began for this interface -- uses the
    cumulative totals directly (hostnet.get_cumulative), NOT a sample sum, since the
    cumulative counter already correctly accounts for every discontinuity-handled delta ever
    recorded, including ones outside any bounded sample-range query's reasonable limits."""
    cumulative = hostnet.get_cumulative(conn, interface)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    if cumulative is None:
        return {
            "label": "all_time", "interface": interface, "period_start": None,
            "period_end": now_utc.isoformat(), "complete": False,
            "rx_bytes": 0, "tx_bytes": 0, "combined_bytes": 0,
            "avg_combined_bytes_per_sec": None, "peak_combined_bytes_per_sec": None,
            "sample_count": 0, "counted_sample_count": 0, "partial_samples": False,
            "coverage_status": COVERAGE_NO_DATA,
            "coverage_note": "Host network monitoring has never recorded a sample for this interface.",
        }
    start = cumulative["first_sample_at"]
    try:
        duration = (now_utc - datetime.fromisoformat(start)).total_seconds()
    except Exception:
        duration = None
    rx, tx = cumulative["cumulative_rx_bytes"], cumulative["cumulative_tx_bytes"]
    avg_bps = (rx + tx) / duration if duration and duration > 0 else None
    return {
        "label": "all_time", "interface": interface, "period_start": start,
        "period_end": now_utc.isoformat(), "complete": False,
        "rx_bytes": rx, "tx_bytes": tx, "combined_bytes": rx + tx,
        "avg_combined_bytes_per_sec": avg_bps, "peak_combined_bytes_per_sec": None,
        "sample_count": None, "counted_sample_count": None, "partial_samples": False,
        "coverage_status": COVERAGE_FULL,
        "coverage_note": None,
    }


def custom_range(conn, interface: str, start_local: datetime, end_local: datetime,
                  tz_name: str = DEFAULT_TIMEZONE) -> dict:
    """Operator-supplied arbitrary range, in the configured local timezone. `end_local` in
    the future is clamped to "now" (never fabricated traffic for a not-yet-elapsed future
    span); `start_local` after `end_local` raises ValueError rather than silently swapping
    them or returning a nonsensical empty/negative-duration result."""
    tz = ZoneInfo(tz_name)
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=tz)
    if end_local.tzinfo is None:
        end_local = end_local.replace(tzinfo=tz)
    if start_local >= end_local:
        raise ValueError("custom_range: start must be strictly before end")
    now_utc = datetime.now(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    complete = True
    if end_utc > now_utc:
        end_utc = now_utc
        complete = False
    start_utc = start_local.astimezone(timezone.utc)
    return _build_period_result(conn, interface, "custom_range", start_utc.isoformat(),
                                 end_utc.isoformat(), complete=complete)


def all_periods(conn, interface: str, tz_name: str = DEFAULT_TIMEZONE, now: datetime = None) -> dict:
    """Convenience bundle of every standard period, keyed by label -- what a dashboard/report
    widget wants in one call, bounded to a fixed, small number of range-queries (never an
    unbounded or per-widget-uncoordinated set of queries)."""
    return {
        "today": today(conn, interface, tz_name, now),
        "yesterday": yesterday(conn, interface, tz_name, now),
        "last_24_hours": last_24_hours(conn, interface, now),
        "current_week": current_week(conn, interface, tz_name, now),
        "last_7_days": last_7_days(conn, interface, now),
        "current_gregorian_month": current_gregorian_month(conn, interface, tz_name, now),
        "previous_gregorian_month": previous_gregorian_month(conn, interface, tz_name, now),
        "current_jalali_month": current_jalali_month(conn, interface, tz_name, now),
        "previous_jalali_month": previous_jalali_month(conn, interface, tz_name, now),
        "all_time": all_time(conn, interface, now),
    }
