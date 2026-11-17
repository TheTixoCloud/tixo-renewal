"""Tests for host_time.py (v1.8.0 Part 8): uptime reading/formatting, boot time estimation,
calendar preference persistence, timezone-aware/DST-correct formatting, Jalali/Gregorian/
auto/both rendering, and honest missing-data handling.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import host_time as ht


def _config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    return str(d)


def _state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return str(d)


# ---------- read_uptime_seconds: real /proc/uptime read ---------------------------------------

def test_read_uptime_seconds_reads_real_proc_uptime():
    """A real smoke test against the actual host -- /proc/uptime exists on every Linux
    system this runs on."""
    result = ht.read_uptime_seconds()
    assert isinstance(result, float)
    assert result > 0


# ---------- format_duration_human --------------------------------------------------------------

def test_format_duration_includes_all_four_units():
    assert ht.format_duration_human(5) == "0d 0h 0m 5s"


def test_format_duration_days_hours_minutes_seconds():
    seconds = 2 * 86400 + 3 * 3600 + 12 * 60 + 45
    assert ht.format_duration_human(seconds) == "2d 3h 12m 45s"


def test_format_duration_zero():
    assert ht.format_duration_human(0) == "0d 0h 0m 0s"


def test_format_duration_negative_is_unavailable():
    assert ht.format_duration_human(-5) == ht.UNAVAILABLE


def test_format_duration_unparseable_is_unavailable():
    assert ht.format_duration_human("not-a-number") == ht.UNAVAILABLE
    assert ht.format_duration_human(None) == ht.UNAVAILABLE


def test_format_duration_exact_day_boundary():
    assert ht.format_duration_human(86400) == "1d 0h 0m 0s"


# ---------- compute_boot_time_utc --------------------------------------------------------------

def test_compute_boot_time_subtracts_uptime_from_now():
    now = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    boot = ht.compute_boot_time_utc(3600, now=now)  # 1 hour uptime
    assert boot == datetime(2026, 1, 1, 23, 0, 0, tzinfo=timezone.utc)


def test_compute_boot_time_unavailable_uptime_returns_none():
    assert ht.compute_boot_time_utc(ht.UNAVAILABLE) is None
    assert ht.compute_boot_time_utc(None) is None


def test_compute_boot_time_negative_uptime_returns_none():
    assert ht.compute_boot_time_utc(-1) is None


def test_get_current_boot_id_reads_real_value():
    boot_id = ht.get_current_boot_id()
    assert isinstance(boot_id, str)
    assert boot_id != ""


# ---------- calendar preference persistence ----------------------------------------------------

def test_load_calendar_preference_defaults_to_auto(tmp_path):
    assert ht.load_calendar_preference(_config_dir(tmp_path)) == ht.CALENDAR_AUTO


def test_save_and_load_calendar_preference_roundtrips(tmp_path):
    config_dir = _config_dir(tmp_path)
    ht.save_calendar_preference(config_dir, ht.CALENDAR_BOTH)
    assert ht.load_calendar_preference(config_dir) == ht.CALENDAR_BOTH


def test_save_calendar_preference_rejects_invalid_value(tmp_path):
    with pytest.raises(ValueError):
        ht.save_calendar_preference(_config_dir(tmp_path), "lunar")


def test_save_calendar_preference_preserves_existing_display_tz(tmp_path):
    import menu as menu_mod
    config_dir = _config_dir(tmp_path)
    menu_mod.save_display_tz(config_dir, "Europe/London")
    ht.save_calendar_preference(config_dir, ht.CALENDAR_GREGORIAN)
    assert menu_mod.load_display_tz(config_dir) == "Europe/London"
    assert ht.load_calendar_preference(config_dir) == ht.CALENDAR_GREGORIAN


def test_load_calendar_preference_malformed_file_falls_back_to_auto(tmp_path):
    config_dir = _config_dir(tmp_path)
    import menu as menu_mod
    path = menu_mod.display_settings_path(config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    assert ht.load_calendar_preference(config_dir) == ht.CALENDAR_AUTO


def test_load_calendar_preference_invalid_stored_value_falls_back_to_auto(tmp_path):
    config_dir = _config_dir(tmp_path)
    import menu as menu_mod
    import json
    path = menu_mod.display_settings_path(config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"calendar_preference": "lunar"}, f)
    assert ht.load_calendar_preference(config_dir) == ht.CALENDAR_AUTO


# ---------- detect_default_display_timezone ----------------------------------------------------

def test_detect_default_display_timezone_falls_back_when_host_tz_unavailable(monkeypatch):
    import hostinfo
    monkeypatch.setattr(hostinfo, "get_host_timezone", lambda: hostinfo.UNAVAILABLE)
    import menu as menu_mod
    assert ht.detect_default_display_timezone() == menu_mod.DEFAULT_DISPLAY_TZ


def test_detect_default_display_timezone_uses_valid_host_tz(monkeypatch):
    import hostinfo
    monkeypatch.setattr(hostinfo, "get_host_timezone", lambda: "Europe/London")
    assert ht.detect_default_display_timezone() == "Europe/London"


def test_detect_default_display_timezone_falls_back_on_invalid_host_tz(monkeypatch):
    import hostinfo
    monkeypatch.setattr(hostinfo, "get_host_timezone", lambda: "Not/ARealZone")
    import menu as menu_mod
    assert ht.detect_default_display_timezone() == menu_mod.DEFAULT_DISPLAY_TZ


# ---------- format_local_datetime: calendar preference + DST ----------------------------------

def test_format_local_datetime_auto_uses_jalali_for_tehran():
    dt = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
    result = ht.format_local_datetime(dt, "Asia/Tehran", ht.CALENDAR_AUTO)
    assert "Jalali" in result


def test_format_local_datetime_auto_uses_gregorian_for_other_tz():
    dt = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
    result = ht.format_local_datetime(dt, "UTC", ht.CALENDAR_AUTO)
    assert "Jalali" not in result
    assert "2026-03-25" in result


def test_format_local_datetime_jalali_forced_for_non_tehran_tz():
    """An explicit administrator override: Jalali calendar even outside Asia/Tehran."""
    dt = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
    result = ht.format_local_datetime(dt, "UTC", ht.CALENDAR_JALALI)
    assert "Jalali" in result


def test_format_local_datetime_gregorian_forced_for_tehran():
    dt = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
    result = ht.format_local_datetime(dt, "Asia/Tehran", ht.CALENDAR_GREGORIAN)
    assert "Jalali" not in result


def test_format_local_datetime_both_shows_both_calendars():
    dt = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
    result = ht.format_local_datetime(dt, "Asia/Tehran", ht.CALENDAR_BOTH)
    assert "2026-03-25" in result
    assert any(month in result for month in
               ["Farvardin", "Esfand", "Ordibehesht"])  # some Jalali month name present


def test_format_local_datetime_uses_24_hour_clock():
    dt = datetime(2026, 1, 1, 15, 30, 0, tzinfo=timezone.utc)  # 15:30 UTC -- would be 3:30 PM in 12h
    result = ht.format_local_datetime(dt, "UTC", ht.CALENDAR_GREGORIAN)
    assert "15:30:00" in result
    assert "PM" not in result and "AM" not in result


def test_format_local_datetime_accepts_iso_string():
    result = ht.format_local_datetime("2026-01-01T12:00:00+00:00", "UTC", ht.CALENDAR_GREGORIAN)
    assert "2026-01-01" in result


def test_format_local_datetime_never_raises_on_garbage_input():
    result = ht.format_local_datetime("not-a-timestamp", "UTC", ht.CALENDAR_GREGORIAN)
    assert result == "not-a-timestamp"


def test_format_local_datetime_crosses_dst_transition_correctly():
    """Europe/London: clocks spring forward on the last Sunday of March. A time just before
    and just after the transition must show correctly different UTC offsets -- this is
    Python's zoneinfo doing the real work; the test guards that this module's thin wrapper
    doesn't break it."""
    before_dst = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)  # before spring-forward
    after_dst = datetime(2026, 3, 29, 2, 30, tzinfo=timezone.utc)   # after spring-forward
    result_before = ht.format_local_datetime(before_dst, "Europe/London", ht.CALENDAR_GREGORIAN)
    result_after = ht.format_local_datetime(after_dst, "Europe/London", ht.CALENDAR_GREGORIAN)
    # before: GMT (UTC+0) -> 00:30; after: BST (UTC+1) -> 03:30
    assert "00:30:00" in result_before
    assert "03:30:00" in result_after


# ---------- get_host_time_snapshot: full bundle, honest missing-data ---------------------------

def test_snapshot_without_interface_omits_network_section(tmp_path):
    snap = ht.get_host_time_snapshot(_config_dir(tmp_path), _state_dir(tmp_path))
    assert snap["network_configured"] is False
    assert "kernel_counter" not in snap
    assert snap["uptime_human"] != ht.UNAVAILABLE
    assert snap["boot_id"] != ""


def test_snapshot_with_interface_but_no_samples_reports_honestly(tmp_path):
    snap = ht.get_host_time_snapshot(_config_dir(tmp_path), _state_dir(tmp_path), interface="lo")
    assert snap["network_configured"] is True
    assert snap["monitoring_activation_local"] == ht.UNAVAILABLE
    assert snap["last_persistent_sample_local"] == ht.UNAVAILABLE
    assert snap["kernel_counter"]["ok"] is True  # lo is a real interface on this host
    assert snap["kernel_counter"]["source"].startswith("/sys/class/net/lo/")


def test_snapshot_with_real_samples_shows_monitoring_activation_and_elapsed(tmp_path):
    import hostnet
    state_dir = _state_dir(tmp_path)
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    hostnet.record_sample(conn, "lo", 0, 0, boot_id="b1", sampled_at="2026-01-01T00:00:00+00:00")
    hostnet.record_sample(conn, "lo", 1000, 2000, boot_id="b1", sampled_at="2026-01-01T00:05:00+00:00")
    conn.close()
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    snap = ht.get_host_time_snapshot(_config_dir(tmp_path), state_dir, interface="lo", now=now)
    assert snap["monitoring_activation_utc"] == "2026-01-01T00:00:00+00:00"
    assert snap["monitoring_elapsed_human"] == "0d 1h 0m 0s"
    assert snap["last_persistent_sample_utc"] == "2026-01-01T00:05:00+00:00"
    assert snap["data_coverage_discontinuity"] is None


def test_snapshot_kernel_counter_never_merged_with_persistent_totals(tmp_path):
    import hostnet
    state_dir = _state_dir(tmp_path)
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    hostnet.record_sample(conn, "lo", 0, 0, boot_id="b1", sampled_at="2026-01-01T00:00:00+00:00")
    hostnet.record_sample(conn, "lo", 999999999, 999999999, boot_id="b1",
                           sampled_at="2026-01-01T00:05:00+00:00")
    conn.close()
    snap = ht.get_host_time_snapshot(_config_dir(tmp_path), state_dir, interface="lo")
    # the live kernel reading for "lo" on THIS host is whatever lo's real counters are --
    # unrelated to the fake 999999999 persistent values just recorded. Confirm they're
    # tracked as separate dict keys, never summed together.
    assert "kernel_counter" in snap
    assert "monitoring_activation_utc" in snap
    assert snap["kernel_counter"]["combined_bytes"] != 999999999 * 2


def test_snapshot_reset_semantics_label_present(tmp_path):
    snap = ht.get_host_time_snapshot(_config_dir(tmp_path), _state_dir(tmp_path), interface="lo")
    assert "reset" in snap["kernel_counter"]["reset_semantics"].lower()
    assert "never" in snap["kernel_counter"]["reset_semantics"].lower()


# ---------- CLI wiring: render_host_info's new section, render_hostnet_panel kernel counter ---

def test_render_host_info_with_host_time_snapshot_shows_new_section(tmp_path):
    import menu
    import hostinfo
    info = hostinfo.collect_host_info()
    info["host_time_snapshot"] = {
        "current_local": "2026-01-01 12:00:00 UTC", "display_timezone": "UTC",
        "uptime_human": "1d 2h 3m 4s", "boot_time_local": "2025-12-31 10:00:00 UTC",
        "boot_id": "abc-123", "network_configured": False,
    }
    out = menu.render_host_info(info, menu.Style(enabled=False))
    assert "SERVER TIME & UPTIME" in out
    assert "1d 2h 3m 4s" in out
    assert "abc-123" in out


def test_render_host_info_without_host_time_snapshot_omits_section(tmp_path):
    """Backward compatible: existing callers that don't pass a snapshot see no new section."""
    import menu
    import hostinfo
    info = hostinfo.collect_host_info()
    out = menu.render_host_info(info, menu.Style(enabled=False))
    assert "SERVER TIME & UPTIME" not in out


def test_render_hostnet_panel_with_kernel_counter_shows_labeled_section():
    import menu
    cumulative = {"cumulative_rx_bytes": 100, "cumulative_tx_bytes": 100, "first_sample_at": None}
    kernel_counter = {"ok": True, "rx_bytes": 5_000_000_000, "tx_bytes": 2_000_000_000,
                       "combined_bytes": 7_000_000_000, "source": "/sys/class/net/eth0/statistics/...",
                       "reset_semantics": "Resets on reboot; never added to persistent totals."}
    out = menu.render_hostnet_panel("eth0", cumulative, None, None, menu.Style(enabled=False),
                                     kernel_counter=kernel_counter)
    assert "KERNEL COUNTER" in out
    assert "5.00 GB" in out
    assert "never added to persistent totals" in out


def test_render_hostnet_panel_without_kernel_counter_omits_section():
    import menu
    cumulative = {"cumulative_rx_bytes": 100, "cumulative_tx_bytes": 100, "first_sample_at": None}
    out = menu.render_hostnet_panel("eth0", cumulative, None, None, menu.Style(enabled=False))
    assert "KERNEL COUNTER" not in out


# ---------- control_center_api / telegram_bot wiring smoke tests ------------------------------

def test_telegram_host_command_includes_uptime_and_boot_time(tmp_path):
    import telegram_bot as bot
    import telegram_config as tc
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    settings = tc.TelegramSettings(admin_ids=[111])
    update = {"message": {"text": "/host", "from": {"id": 111}, "chat": {"id": 1}}}
    result = bot.process_update(update, settings, config_dir, state_dir)
    assert "Server local time" in result["reply"]
    assert "Last boot" in result["reply"]
    assert "Boot ID" in result["reply"]


def test_telegram_status_command_includes_server_time(tmp_path):
    import telegram_bot as bot
    import telegram_config as tc
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    settings = tc.TelegramSettings(admin_ids=[111])
    update = {"message": {"text": "/status", "from": {"id": 111}, "chat": {"id": 1}}}
    result = bot.process_update(update, settings, config_dir, state_dir)
    assert "Server time" in result["reply"]
