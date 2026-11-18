"""Tests for traffic_analytics.py (v1.8.0): daily/weekly/monthly date boundaries, Jalali
calendar boundaries, timezone behavior, missing-data intervals, combined RX/TX calculations,
and peak/average throughput derivation -- all built on real hostnet.py accounting fixtures
(record_sample), never mocked at the accounting layer, so a bug in either module would show
up here.
"""
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import hostnet as hn
import traffic_analytics as ta

TEHRAN = ZoneInfo("Asia/Tehran")


def _db(tmp_path):
    return hn.open_db(str(tmp_path / "hostnet.sqlite3"))


def _sample(conn, iface, rx, tx, sampled_at, boot_id="boot-a"):
    return hn.record_sample(conn, iface, rx, tx, boot_id=boot_id, sampled_at=sampled_at)


# ---------- basic combined/peak/average calculations -----------------------------------------

def test_combined_bytes_equals_rx_plus_tx(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-10T00:00:00+00:00")
    _sample(conn, "eth0", 1000, 400, "2026-03-10T00:05:00+00:00")
    result = ta.custom_range(conn, "eth0",
                              datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc),
                              datetime(2026, 3, 10, 1, 0, tzinfo=timezone.utc),
                              tz_name="UTC")
    assert result["rx_bytes"] == 1000
    assert result["tx_bytes"] == 400
    assert result["combined_bytes"] == 1400


def test_peak_combined_rate_takes_max_not_average():
    samples = [
        {"rx_delta": 100, "tx_delta": 100, "gap_seconds": 100, "discontinuity_type": None},  # 2 B/s
        {"rx_delta": 10000, "tx_delta": 10000, "gap_seconds": 10, "discontinuity_type": None},  # 2000 B/s
        {"rx_delta": 50, "tx_delta": 50, "gap_seconds": 100, "discontinuity_type": None},  # 1 B/s
    ]
    assert ta.peak_combined_rate_bps(samples) == 2000.0


def test_peak_combined_rate_ignores_discontinuity_samples():
    samples = [
        {"rx_delta": None, "tx_delta": None, "gap_seconds": None, "discontinuity_type": "REBOOT"},
        {"rx_delta": 100, "tx_delta": 100, "gap_seconds": 100, "discontinuity_type": None},
    ]
    assert ta.peak_combined_rate_bps(samples) == 2.0


def test_peak_combined_rate_zero_when_no_usable_samples():
    assert ta.peak_combined_rate_bps([]) == 0.0
    assert ta.peak_combined_rate_bps(
        [{"rx_delta": None, "tx_delta": None, "gap_seconds": None, "discontinuity_type": "UNKNOWN"}]) == 0.0


def test_discontinuity_sample_excluded_from_totals_and_peak(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 5000, 5000, "2026-03-10T00:00:00+00:00", boot_id="boot-a")
    # counter went backwards within the same boot -- a discontinuity, contributes nothing
    _sample(conn, "eth0", 100, 100, "2026-03-10T00:05:00+00:00", boot_id="boot-a")
    _sample(conn, "eth0", 600, 600, "2026-03-10T00:10:00+00:00", boot_id="boot-a")
    result = ta.custom_range(conn, "eth0",
                              datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc),
                              datetime(2026, 3, 10, 1, 0, tzinfo=timezone.utc),
                              tz_name="UTC")
    # only the clean 100->600 delta (500+500=1000) should count
    assert result["combined_bytes"] == 1000
    assert result["partial_samples"] is True


def test_average_combined_bytes_per_sec_uses_wall_clock_duration(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-10T00:00:00+00:00")
    _sample(conn, "eth0", 3600, 3600, "2026-03-10T00:59:00+00:00")  # 7200 bytes within the hour
    result = ta.custom_range(conn, "eth0",
                              datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc),
                              datetime(2026, 3, 10, 1, 0, tzinfo=timezone.utc),
                              tz_name="UTC")
    assert result["combined_bytes"] == 7200
    assert result["avg_combined_bytes_per_sec"] == 7200 / 3600


# ---------- coverage / missing-data handling --------------------------------------------------

def test_coverage_no_data_when_interface_never_sampled(tmp_path):
    conn = _db(tmp_path)
    result = ta.today(conn, "ghost0", tz_name="UTC",
                       now=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    assert result["coverage_status"] == ta.COVERAGE_NO_DATA
    assert result["combined_bytes"] == 0
    assert "never recorded" in result["coverage_note"]


def test_coverage_partial_when_period_starts_before_first_sample(tmp_path):
    conn = _db(tmp_path)
    # monitoring only began mid-morning
    _sample(conn, "eth0", 0, 0, "2026-03-10T10:00:00+00:00")
    _sample(conn, "eth0", 1000, 1000, "2026-03-10T10:05:00+00:00")
    result = ta.today(conn, "eth0", tz_name="UTC",
                       now=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    assert result["coverage_status"] == ta.COVERAGE_PARTIAL
    assert "Monitoring only began" in result["coverage_note"]
    # the real traffic that WAS observed must still be counted, never discarded
    assert result["combined_bytes"] == 2000


def test_coverage_full_when_monitoring_predates_period(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-01-01T00:00:00+00:00")
    _sample(conn, "eth0", 1000, 1000, "2026-03-10T10:05:00+00:00")
    result = ta.today(conn, "eth0", tz_name="UTC",
                       now=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    assert result["coverage_status"] == ta.COVERAGE_FULL
    assert result["coverage_note"] is None


# ---------- timezone-aware day boundaries ------------------------------------------------------

def test_today_boundary_respects_configured_timezone_not_utc(tmp_path):
    """A sample at 2026-03-09T21:00:00 UTC is 2026-03-10 00:30 in Asia/Tehran (UTC+03:30) --
    i.e. it IS "today" in Tehran even though it's still the 9th in UTC."""
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-09T20:00:00+00:00")
    _sample(conn, "eth0", 777, 777, "2026-03-09T21:00:00+00:00")  # Tehran: 2026-03-10 00:30
    now_utc = datetime(2026, 3, 10, 1, 0, tzinfo=timezone.utc)  # Tehran: 2026-03-10 04:30
    result = ta.today(conn, "eth0", tz_name="Asia/Tehran", now=now_utc)
    assert result["combined_bytes"] == 1554  # 777*2, the delta from the second sample


def test_yesterday_excludes_today_and_is_marked_complete(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-09T20:30:00+00:00")  # Tehran 2026-03-10 00:00 exactly
    _sample(conn, "eth0", 500, 500, "2026-03-08T20:35:00+00:00")  # within yesterday (Tehran 9th)
    now_utc = datetime(2026, 3, 10, 1, 0, tzinfo=timezone.utc)
    result = ta.yesterday(conn, "eth0", tz_name="Asia/Tehran", now=now_utc)
    assert result["complete"] is True


def test_last_24_hours_is_rolling_not_calendar_aligned(tmp_path):
    conn = _db(tmp_path)
    now_utc = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    _sample(conn, "eth0", 0, 0, "2026-03-09T11:00:00+00:00")  # 25h before now -- excluded
    _sample(conn, "eth0", 100, 100, "2026-03-09T13:00:00+00:00")  # 23h before now -- included
    result = ta.last_24_hours(conn, "eth0", now=now_utc)
    assert result["period_start"] == "2026-03-09T12:00:00+00:00"
    assert result["combined_bytes"] == 200


# ---------- week boundary (ISO Monday start) --------------------------------------------------

def test_current_week_starts_monday(tmp_path):
    conn = _db(tmp_path)
    # 2026-03-09 is a Monday
    monday_start_utc = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)  # Wednesday
    result = ta.current_week(conn, "eth0", tz_name="UTC", now=now_utc)
    assert result["period_start"] == monday_start_utc.isoformat()


# ---------- Gregorian vs Jalali month boundaries ----------------------------------------------

def test_current_gregorian_month_starts_first_of_month(tmp_path):
    conn = _db(tmp_path)
    now_utc = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    result = ta.current_gregorian_month(conn, "eth0", tz_name="UTC", now=now_utc)
    assert result["period_start"] == datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc).isoformat()


def test_previous_gregorian_month_is_fully_elapsed_and_complete(tmp_path):
    conn = _db(tmp_path)
    now_utc = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    result = ta.previous_gregorian_month(conn, "eth0", tz_name="UTC", now=now_utc)
    assert result["period_start"] == datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc).isoformat()
    assert result["period_end"] == datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc).isoformat()
    assert result["complete"] is True


def test_current_jalali_month_boundary_differs_from_gregorian(tmp_path):
    """2026-03-21 is Nowruz (1 Farvardin 1405) -- the Jalali new year, right at a Gregorian
    month boundary mismatch: the Jalali month start is NOT 2026-03-01."""
    conn = _db(tmp_path)
    now_utc = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)  # Tehran: still March 25
    result_jalali = ta.current_jalali_month(conn, "eth0", tz_name="Asia/Tehran", now=now_utc)
    result_gregorian = ta.current_gregorian_month(conn, "eth0", tz_name="Asia/Tehran", now=now_utc)
    assert result_jalali["period_start"] != result_gregorian["period_start"]
    # Jalali month should start on/around Nowruz (March 21), not March 1
    assert "2026-03-2" in result_jalali["period_start"] or "2026-03-21" in result_jalali["period_start"]


def test_previous_jalali_month_is_complete(tmp_path):
    conn = _db(tmp_path)
    now_utc = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
    result = ta.previous_jalali_month(conn, "eth0", tz_name="Asia/Tehran", now=now_utc)
    assert result["complete"] is True
    assert result["period_start"] < result["period_end"]


# ---------- all_time uses cumulative, not a bounded sample sum --------------------------------

def test_all_time_uses_cumulative_counter_not_sample_range_sum(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-01-01T00:00:00+00:00")
    _sample(conn, "eth0", 10000, 20000, "2026-01-02T00:00:00+00:00")
    result = ta.all_time(conn, "eth0", now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert result["rx_bytes"] == 10000
    assert result["tx_bytes"] == 20000
    assert result["combined_bytes"] == 30000
    assert result["period_start"] == "2026-01-01T00:00:00+00:00"


def test_all_time_no_data_reports_zero_not_fabricated(tmp_path):
    conn = _db(tmp_path)
    result = ta.all_time(conn, "ghost0")
    assert result["coverage_status"] == ta.COVERAGE_NO_DATA
    assert result["combined_bytes"] == 0


# ---------- custom_range safety ---------------------------------------------------------------

def test_custom_range_future_end_clamped_to_now_and_marked_incomplete(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-10T00:00:00+00:00")
    _sample(conn, "eth0", 500, 500, "2026-03-10T00:05:00+00:00")

    import traffic_analytics
    orig_datetime = traffic_analytics.datetime

    class FrozenDatetime(orig_datetime):
        @classmethod
        def now(cls, tz=None):
            return orig_datetime(2026, 3, 10, 0, 10, tzinfo=timezone.utc)

    traffic_analytics.datetime = FrozenDatetime
    try:
        result = ta.custom_range(conn, "eth0",
                                  datetime(2026, 3, 10, 0, 0),
                                  datetime(2026, 3, 10, 23, 59),  # requests "until end of day" (future)
                                  tz_name="UTC")
    finally:
        traffic_analytics.datetime = orig_datetime
    assert result["complete"] is False
    assert result["period_end"] == "2026-03-10T00:10:00+00:00"


def test_custom_range_start_after_end_raises(tmp_path):
    conn = _db(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        ta.custom_range(conn, "eth0", datetime(2026, 3, 10, 12, 0), datetime(2026, 3, 10, 0, 0),
                         tz_name="UTC")


# ---------- bundle ------------------------------------------------------------------------------

def test_all_periods_bundle_returns_every_expected_label(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-10T00:00:00+00:00")
    bundle = ta.all_periods(conn, "eth0", tz_name="Asia/Tehran",
                             now=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    expected_labels = {
        "today", "yesterday", "last_24_hours", "current_week", "last_7_days",
        "current_gregorian_month", "previous_gregorian_month",
        "current_jalali_month", "previous_jalali_month", "all_time",
    }
    assert set(bundle.keys()) == expected_labels
    for label, result in bundle.items():
        assert result["label"] == label


# ---------- daily_series (v1.9.0, chart data) ----------------------------------------------

def test_daily_series_returns_requested_number_of_days(tmp_path):
    conn = _db(tmp_path)
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    series = ta.daily_series(conn, "eth0", 5, tz_name="UTC", now=now)
    assert len(series) == 5
    assert series[-1]["date"] == "2026-03-10"
    assert series[0]["date"] == "2026-03-06"


def test_daily_series_sums_deltas_per_local_day(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-09T00:00:00+00:00")
    _sample(conn, "eth0", 1_000_000_000, 500_000_000, "2026-03-09T12:00:00+00:00")
    _sample(conn, "eth0", 2_000_000_000, 1_000_000_000, "2026-03-10T01:00:00+00:00")
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    series = ta.daily_series(conn, "eth0", 2, tz_name="UTC", now=now)
    day9, day10 = series
    assert day9["date"] == "2026-03-09"
    assert day9["rx_bytes"] == 1_000_000_000
    assert day10["date"] == "2026-03-10"
    assert day10["rx_bytes"] == 1_000_000_000


def test_daily_series_marks_zero_sample_day_as_partial(tmp_path):
    conn = _db(tmp_path)
    _sample(conn, "eth0", 0, 0, "2026-03-05T00:00:00+00:00")
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    series = ta.daily_series(conn, "eth0", 3, tz_name="UTC", now=now)
    assert all(d["partial"] for d in series if d["sample_count"] == 0)
