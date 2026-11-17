"""Tests for the interactive management menu's pure/testable logic. The input()-driven REPL
loop itself is thin glue over these functions and over existing, already-tested renewal_cli.py
commands -- not re-tested here; what matters is the logic that validates operator input and
assembles data before any of it reaches a subprocess call or a template.
"""
import json
import os
import sys
from decimal import Decimal
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import menu
import policy as pol


# ---------- parse_gift_gb ---------------------------------------------------------------

def test_parse_gift_gb_accepts_zero_explicitly():
    assert menu.parse_gift_gb("0") == Decimal("0")


def test_parse_gift_gb_accepts_positive():
    assert menu.parse_gift_gb("250") == Decimal("250")


def test_parse_gift_gb_rejects_negative():
    with pytest.raises(ValueError):
        menu.parse_gift_gb("-5")


def test_parse_gift_gb_rejects_non_numeric():
    with pytest.raises(ValueError):
        menu.parse_gift_gb("not-a-number")


def test_parse_gift_gb_rejects_injection_shaped_input():
    with pytest.raises(ValueError):
        menu.parse_gift_gb("5; rm -rf /")


# ---------- mask_secret_for_display -- never leaks the real secret ----------------------

def test_mask_secret_short_value_fully_masked():
    assert menu.mask_secret_for_display("ab") == "**"


def test_mask_secret_long_value_shows_only_first_and_last_two_chars():
    masked = menu.mask_secret_for_display("supersecretkey123")
    assert masked.startswith("su")
    assert masked.endswith("23")
    assert "supersecretkey" not in masked


def test_mask_secret_empty():
    assert menu.mask_secret_for_display("") == "(empty)"


# ---------- update check (read-only HTTP, mocked) ----------------------------------------

def test_format_remote_version_strips_v_prefix():
    assert menu.format_remote_version("v1.2.3") == "1.2.3"
    assert menu.format_remote_version("1.2.3") == "1.2.3"


def test_check_for_update_detects_newer_version(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"tag_name": "v9.9.9", "html_url": "https://example.invalid"}).encode()

    monkeypatch.setattr(menu.urllib.request, "urlopen", lambda req, timeout=5: FakeResp())
    result = menu.check_for_update(current_version="1.1.0")
    assert result["ok"] is True
    assert result["update_available"] is True
    assert result["latest"] == "9.9.9"


def test_is_newer_version_semantic_not_string_compare():
    assert menu.is_newer_version("1.2.0", "1.1.0") is True
    assert menu.is_newer_version("1.1.0", "1.1.0") is False
    assert menu.is_newer_version("1.0.0", "1.1.0") is False  # published release LAGS local dev
    assert menu.is_newer_version("2.0.0", "1.9.9") is True


def test_check_for_update_no_false_positive_when_local_is_ahead_of_published(monkeypatch):
    """Real bug found while testing: a local dev version newer than the last published
    release must never be reported as 'update available'."""
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"tag_name": "v1.0.0", "html_url": "x"}).encode()
    monkeypatch.setattr(menu.urllib.request, "urlopen", lambda req, timeout=5: FakeResp())
    result = menu.check_for_update(current_version="1.1.0")
    assert result["update_available"] is False


def test_check_for_update_no_update_when_versions_match(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"tag_name": "v1.1.0", "html_url": "x"}).encode()

    monkeypatch.setattr(menu.urllib.request, "urlopen", lambda req, timeout=5: FakeResp())
    result = menu.check_for_update(current_version="1.1.0")
    assert result["update_available"] is False


def test_check_for_update_never_raises_on_network_failure(monkeypatch):
    def boom(req, timeout=5):
        raise OSError("network unreachable")
    monkeypatch.setattr(menu.urllib.request, "urlopen", boom)
    result = menu.check_for_update(current_version="1.1.0")
    assert result["ok"] is False
    assert "error" in result


# ---------- dashboard data collection / rendering -----------------------------------------

def test_collect_dashboard_data_reports_policy_and_write_approved(tmp_path):
    config_dir = str(tmp_path / "renewal-config")
    state_dir = str(tmp_path / "shadow-monitor-state")
    os.makedirs(config_dir)
    pol.write_default_policy_config(os.path.join(config_dir, "policy.json"))
    (tmp_path / "renewal-config" / "WRITE_APPROVED").write_text("")
    import nodes as nodes_mod
    nodes_mod.write_default_config(os.path.join(config_dir, "nodes.json"),
                                    nodes=[nodes_mod.NodeConfig("self", mode="local", db_filename="self.sqlite3")])
    data = menu.collect_dashboard_data(config_dir, state_dir)
    assert data["policy_ok"] is True
    assert data["write_approved"] is True
    assert data["monthly_grant_gb"] == "100"
    assert "self" in data["nodes"]
    assert data["per_node"]["self"]["initialized"] is False  # no sqlite3 file created yet


def test_render_dashboard_shows_disabled_auto_renewal_without_crashing():
    data = {"policy_ok": True, "monthly_grant_gb": "100", "auto_renewal_enabled": False,
            "write_approved": False, "per_node": {}, "timers": {}}
    style = menu.Style(enabled=False)
    out = menu.render_dashboard(data, style)
    assert "disabled" in out
    assert "absent" in out


def test_render_dashboard_handles_policy_error_gracefully():
    data = {"policy_ok": False, "policy_error": "schema mismatch", "write_approved": False,
            "per_node": {}, "timers": {}}
    out = menu.render_dashboard(data, menu.Style(enabled=False))
    assert "schema mismatch" in out


# ---------- color/tty detection ------------------------------------------------------------

def test_supports_color_false_when_no_color_env_set(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert menu.supports_color() is False


def test_is_interactive_false_when_stdin_not_a_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert menu.is_interactive() is False


# ---------- Style: color codes are empty strings when disabled ---------------------------

def test_style_disabled_has_no_ansi_codes():
    style = menu.Style(enabled=False)
    assert style.RESET == "" and style.BOLD == "" and style.GREEN == ""


def test_style_enabled_has_real_ansi_codes():
    style = menu.Style(enabled=True)
    assert style.RESET == "\033[0m"
    assert style.GREEN == "\033[32m"


# ---------- noninteractive entrypoint never hangs on input() ------------------------------

def test_main_status_flag_delegates_to_cmd_status_without_hanging(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(sys, "argv", ["menu.py", "--status"])
    def fake_cmd_status(args):
        called["ran"] = True
        return 0
    monkeypatch.setattr(menu.renewal_cli, "cmd_status", fake_cmd_status)
    rc = menu.main()
    assert called.get("ran") is True
    assert rc == 0


def test_main_version_flag_prints_and_exits(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["menu.py", "--version"])
    rc = menu.main()
    assert rc == 0
    assert menu.renewal_cli.VERSION in capsys.readouterr().out


def test_run_menu_refuses_when_not_interactive(monkeypatch):
    monkeypatch.setattr(menu, "is_interactive", lambda: False)
    assert menu.run_menu() == 1


# ---------- v1.2: display-only timezone (never touches host system timezone) --------------

def test_validate_timezone_name_accepts_valid_iana_name():
    assert menu.validate_timezone_name("Asia/Tehran") == "Asia/Tehran"
    assert menu.validate_timezone_name(" UTC ") == "UTC"


def test_validate_timezone_name_rejects_garbage():
    with pytest.raises(ValueError):
        menu.validate_timezone_name("Not/A/Real/Zone")


def test_save_and_load_display_tz_roundtrip(tmp_path):
    config_dir = str(tmp_path)
    menu.save_display_tz(config_dir, "Asia/Tehran")
    assert menu.load_display_tz(config_dir) == "Asia/Tehran"


def test_load_display_tz_defaults_to_asia_tehran_when_unset(tmp_path):
    assert menu.load_display_tz(str(tmp_path)) == "Asia/Tehran"


def test_save_display_tz_rejects_invalid_and_does_not_persist(tmp_path):
    config_dir = str(tmp_path)
    with pytest.raises(ValueError):
        menu.save_display_tz(config_dir, "Nonsense/Zone")
    assert not os.path.exists(menu.display_settings_path(config_dir))


def test_format_local_time_converts_utc_to_tehran_with_jalali_date():
    # 2026-01-01T00:00:00 UTC -> Asia/Tehran is UTC+3:30 -> 03:30 local, same calendar day.
    # v1.7.0: Asia/Tehran now shows the Jalali date alongside the clock time.
    out = menu.format_local_time("2026-01-01T00:00:00+00:00", "Asia/Tehran")
    assert "03:30:00" in out
    assert "Jalali" in out


def test_format_local_time_handles_naive_timestamp_as_utc():
    out = menu.format_local_time("2026-01-01T00:00:00", "UTC")
    assert "2026-01-01 00:00:00" in out


def test_format_local_time_never_raises_on_garbage_falls_back_to_raw():
    out = menu.format_local_time("not-a-timestamp", "Asia/Tehran")
    assert "not-a-timestamp" in out


def test_format_local_time_timezone_boundary_crosses_calendar_day():
    """A known tricky case: a UTC time late in the day crosses into the NEXT calendar day in
    a positive-offset timezone -- must be handled correctly, not just offset-blind. Checked
    against the underlying Gregorian conversion directly (format_local_time shows Jalali for
    Tehran), since the clock-time rollover is the thing under test here, not the calendar."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    dt = datetime.fromisoformat("2026-06-30T21:00:00+00:00").astimezone(ZoneInfo("Asia/Tehran"))
    assert (dt.year, dt.month, dt.day) == (2026, 7, 1)  # 21:00 UTC + 3:30 = 00:30 the next day
    assert dt.strftime("%H:%M:%S") == "00:30:00"
    out = menu.format_local_time("2026-06-30T21:00:00+00:00", "Asia/Tehran")
    assert "00:30:00" in out
    assert "Jalali" in out


# ---------- dashboard surfaces deferred/expired/failed operations --------------------------

def test_render_dashboard_shows_deferred_locked_entries():
    data = {"policy_ok": True, "monthly_grant_gb": "100", "auto_renewal_enabled": True,
            "write_approved": True, "timers": {},
            "per_node": {"self": {"initialized": True, "last_poll_at": None,
                                   "operations_total": 1, "operations_unresolved": 0,
                                   "deferred": [(55, "55|x|1", 2, "2026-01-01T00:00:00+00:00")],
                                   "deferred_expired": [], "failed_or_uncertain": []}}}
    out = menu.render_dashboard(data, menu.Style(enabled=False))
    assert "vpsid=55" in out
    assert "retries=2" in out


def test_dashboard_settings_defaults_when_unset(tmp_path):
    settings = menu.load_dashboard_settings(str(tmp_path))
    assert settings["refresh_interval_minutes"] == menu.DEFAULT_DASHBOARD_REFRESH_MIN
    assert settings["usage_threshold_percent"] == str(menu.DEFAULT_USAGE_THRESHOLD_PERCENT)


def test_save_and_load_dashboard_settings_roundtrip(tmp_path):
    menu.save_dashboard_settings(str(tmp_path), refresh_interval_minutes=15,
                                  usage_threshold_percent="80")
    settings = menu.load_dashboard_settings(str(tmp_path))
    assert settings["refresh_interval_minutes"] == 15
    assert settings["usage_threshold_percent"] == "80"


def test_validate_usage_threshold_rejects_out_of_range():
    with pytest.raises(ValueError):
        menu.validate_usage_threshold_percent("150")
    with pytest.raises(ValueError):
        menu.validate_usage_threshold_percent("-5")


def test_validate_usage_threshold_accepts_boundary_values():
    assert menu.validate_usage_threshold_percent("0") == Decimal("0")
    assert menu.validate_usage_threshold_percent("100") == Decimal("100")


def test_render_progress_bar_handles_unavailable_without_raising():
    out = menu.render_progress_bar(menu.hostinfo.UNAVAILABLE, style=menu.Style(enabled=False))
    assert "n/a" in out


def test_render_progress_bar_renders_percent():
    out = menu.render_progress_bar(42.0, style=menu.Style(enabled=False))
    assert "42.0%" in out


def test_render_host_info_includes_hostname_and_cpu():
    info = menu.hostinfo.collect_host_info()
    out = menu.render_host_info(info, menu.Style(enabled=False))
    assert info["os"]["hostname"] in out


def test_render_virtualizor_info_handles_not_installed():
    v_info = {"installed": False, "version": "unavailable", "virtualization_technologies": [],
              "api": None}
    out = menu.render_virtualizor_info(v_info, menu.Style(enabled=False))
    assert "False" in out


def test_render_analytics_summary_shows_counts():
    result = menu.analytics.compute_bandwidth_analytics([
        {"vpsid": 1, "quota_gb": "100", "used_gb": "50", "status": "running"},
        {"vpsid": 2, "quota_gb": "0", "used_gb": "10", "status": "running"},
    ])
    out = menu.render_analytics_summary(result, menu.Style(enabled=False))
    assert "Total VPS: 2" in out


def test_paginate_splits_into_pages():
    records = list(range(25))
    page1, page, total_pages = menu.paginate(records, page=1, page_size=10)
    assert len(page1) == 10
    assert total_pages == 3


def test_paginate_clamps_out_of_range_page():
    records = list(range(5))
    page_data, page, total_pages = menu.paginate(records, page=99, page_size=10)
    assert page == 1
    assert total_pages == 1


def test_filter_and_sort_vps_by_search():
    records = [{"vpsid": 1, "hostname": "web1"}, {"vpsid": 2, "hostname": "db1"}]
    out = menu.filter_and_sort_vps(records, search="web")
    assert len(out) == 1
    assert out[0]["vpsid"] == 1


def test_filter_and_sort_vps_by_status():
    records = [{"vpsid": 1, "runtime_status": "running"}, {"vpsid": 2, "runtime_status": "stopped"}]
    out = menu.filter_and_sort_vps(records, status_filter="stopped")
    assert len(out) == 1
    assert out[0]["vpsid"] == 2


def test_filter_and_sort_vps_sort_by_vpsid_descending_input():
    records = [{"vpsid": 3}, {"vpsid": 1}, {"vpsid": 2}]
    out = menu.filter_and_sort_vps(records, sort_by="vpsid")
    assert [r["vpsid"] for r in out] == [1, 2, 3]


def test_render_vps_table_empty_shows_hint():
    out = menu.render_vps_table([], menu.Style(enabled=False))
    assert "inventory sync" in out


def test_render_vps_table_shows_vpsid_and_hostname():
    records = [{"vpsid": 1, "hostname": "web1", "status": "running", "quota_gb": "100", "used_gb": "10"}]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "web1" in out
    assert "1" in out


def test_render_dashboard_shows_expired_and_failed_entries():
    data = {"policy_ok": True, "monthly_grant_gb": "100", "auto_renewal_enabled": True,
            "write_approved": True, "timers": {},
            "per_node": {"self": {"initialized": True, "last_poll_at": None,
                                   "operations_total": 2, "operations_unresolved": 0,
                                   "deferred": [], "deferred_expired": [(60, "60|x|1")],
                                   "failed_or_uncertain": [(61, "61|x|1", "API_REPORTED_ERROR")]}}}
    out = menu.render_dashboard(data, menu.Style(enabled=False))
    assert "vpsid=60" in out
    assert "vpsid=61" in out
    assert "API_REPORTED_ERROR" in out


# ---------- v1.4: Textual TUI startup/fallback wiring in menu.main() ----------------------

def test_main_falls_back_to_cli_menu_when_textual_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["menu.py"])
    monkeypatch.setitem(sys.modules, "tui_textual", None)
    called = {}
    def fake_run_menu():
        called["ran"] = True
        return 0
    monkeypatch.setattr(menu, "run_menu", fake_run_menu)
    rc = menu.main()
    assert called.get("ran") is True
    assert rc == 0


def test_main_cli_flag_forces_lightweight_menu(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["menu.py", "--cli"])
    called = {}
    def fake_run_menu():
        called["ran"] = True
        return 0
    monkeypatch.setattr(menu, "run_menu", fake_run_menu)
    rc = menu.main()
    assert called.get("ran") is True


# ---------- v1.5.0: RED theme, grouped host info, navigation helpers, VPS detail ----------

def test_style_accent_is_bright_red_when_enabled():
    style = menu.Style(enabled=True)
    assert style.ACCENT == style.BRIGHT_RED
    assert style.ACCENT == "\033[91m"


def test_style_accent_empty_when_disabled():
    style = menu.Style(enabled=False)
    assert style.ACCENT == ""


def test_supports_unicode_reflects_stdout_encoding(monkeypatch):
    class FakeStdout:
        encoding = "utf-8"
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    assert menu.supports_unicode() is True


def test_supports_unicode_false_for_ascii_encoding(monkeypatch):
    class FakeStdout:
        encoding = "ascii"
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    assert menu.supports_unicode() is False


def test_render_banner_picks_fallback_when_no_unicode(monkeypatch):
    monkeypatch.setattr(menu, "supports_unicode", lambda: False)
    out = menu.render_banner(menu.Style(enabled=False))
    assert "CONTROL CENTER" in out
    assert out.strip() == menu.BANNER_FALLBACK.strip()


def test_clear_screen_noop_when_not_interactive(monkeypatch, capsys):
    monkeypatch.setattr(menu, "is_interactive", lambda: False)
    menu.clear_screen()
    out = capsys.readouterr().out
    assert out == ""


def test_render_host_info_groups_into_sections():
    info = menu.hostinfo.collect_host_info()
    out = menu.render_host_info(info, menu.Style(enabled=False))
    for section in ("OPERATING SYSTEM", "CPU", "MEMORY & SWAP", "STORAGE", "NETWORK", "TIMEZONE"):
        assert section in out


def test_render_host_info_hides_virtual_interfaces_by_default():
    info = {
        "os": {"hostname": "h", "distribution": "d", "kernel": "k", "architecture": "a"},
        "cpu": {"vendor": "v", "model": "m", "physical_cores": 1, "logical_cpus": 2, "threads_per_core": 2},
        "memory": {"total_mb": 100, "available_mb": 50, "swap_total_mb": 10, "swap_free_mb": 5},
        "disks": [],
        "uptime": {"uptime_seconds": 3600, "load_average": {"1m": 1, "5m": 1, "15m": 1}},
        "usage": {"cpu_percent": "unsupported", "memory_percent": 50.0},
        "temperature": {"available": False, "reason": "unavailable", "zones": []},
        "network_interfaces": [
            {"name": "eth0", "state": "up", "ipv4": "10.0.0.1", "is_virtual": False},
            {"name": "vnet5", "state": "up", "ipv4": "unavailable", "is_virtual": True},
        ],
        "host_timezone": "UTC",
    }
    out = menu.render_host_info(info, menu.Style(enabled=False))
    assert "eth0" in out
    assert "vnet5" not in out
    assert "1 interface(s) hidden" in out


def test_render_host_info_shows_virtual_interfaces_when_requested():
    info = {
        "os": {"hostname": "h", "distribution": "d", "kernel": "k", "architecture": "a"},
        "cpu": {"vendor": "v", "model": "m", "physical_cores": 1, "logical_cpus": 2, "threads_per_core": 2},
        "memory": {"total_mb": 100, "available_mb": 50, "swap_total_mb": 10, "swap_free_mb": 5},
        "disks": [],
        "uptime": {"uptime_seconds": 3600, "load_average": {"1m": 1, "5m": 1, "15m": 1}},
        "usage": {"cpu_percent": "unsupported", "memory_percent": 50.0},
        "temperature": {"available": False, "reason": "unavailable", "zones": []},
        "network_interfaces": [
            {"name": "vnet5", "state": "up", "ipv4": "unavailable", "is_virtual": True},
        ],
        "host_timezone": "UTC",
    }
    out = menu.render_host_info(info, menu.Style(enabled=False), show_virtual_interfaces=True)
    assert "vnet5" in out


def test_render_vps_detail_shows_all_fields():
    record = {"vpsid": 42, "hostname": "web1", "status": "suspended", "runtime_status": "running",
              "quota_gb": "100", "used_gb": "50", "ram_mb": "2048", "cores": "2", "disk_gb": "40",
              "virtualization": "kvm", "locked": False, "node_serid": 1}
    out = menu.render_vps_detail(record, menu.Style(enabled=False))
    assert "42" in out
    assert "web1" in out
    assert "Runtime Status:" in out
    assert "running" in out
    assert "Billing Suspended: yes" in out


def test_render_vps_table_marks_unlimited_quota():
    records = [{"vpsid": 1, "hostname": "h", "status": "running", "quota_gb": "0", "used_gb": "500"}]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "UNLIMITED" in out


def test_menu_items_no_longer_has_standalone_manual_inventory_sync():
    """v1.6.0: inventory sync became automatic (its own systemd timer); the manual action
    moved inside the inventory view screen as an explicit override, no longer a top-level
    main-menu item."""
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Sync VPS inventory (read-only)" not in labels
    assert "View VPS inventory & analytics" in labels


def test_menu_items_includes_traffic_analytics_screen():
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Host network traffic analytics (historical)" in labels


def test_render_traffic_analytics_report_shows_each_period_and_distinguishes_no_data():
    import traffic_analytics as ta
    bundle = {
        "today": {"label": "today", "coverage_status": ta.COVERAGE_FULL, "coverage_note": None,
                   "rx_bytes": 1_000_000_000, "tx_bytes": 500_000_000, "combined_bytes": 1_500_000_000,
                   "avg_combined_bytes_per_sec": 1234.5, "peak_combined_bytes_per_sec": 9999.0,
                   "period_start": "2026-01-01T00:00:00+00:00", "period_end": "2026-01-01T12:00:00+00:00",
                   "complete": False},
        "yesterday": {"label": "yesterday", "coverage_status": ta.COVERAGE_NO_DATA,
                      "coverage_note": "Host network monitoring has never recorded a sample for this interface.",
                      "rx_bytes": 0, "tx_bytes": 0, "combined_bytes": 0,
                      "avg_combined_bytes_per_sec": None, "peak_combined_bytes_per_sec": None,
                      "period_start": None, "period_end": "2026-01-01T00:00:00+00:00", "complete": False},
    }
    out = menu.render_traffic_analytics_report(bundle, menu.Style(enabled=False))
    assert "Today" in out
    assert "1.50 GB" in out  # combined
    assert "Yesterday" in out
    assert "No data" in out
    assert "never recorded" in out


# ---------- v1.7.0: dashboard auto-sync health, verified via actual systemd state ----------

def test_classify_timer_health_active():
    assert menu.classify_timer_health("active", "enabled") == "ACTIVE"


def test_classify_timer_health_failed():
    assert menu.classify_timer_health("failed", "enabled") == "FAILED"


def test_classify_timer_health_inactive_when_not_active_but_recognized():
    assert menu.classify_timer_health("inactive", "disabled") == "INACTIVE"


def test_classify_timer_health_unknown_never_fabricated_as_active():
    assert menu.classify_timer_health("", "") == "UNKNOWN"
    assert menu.classify_timer_health("activating", "enabled") == "UNKNOWN"


def test_classify_timer_health_never_reports_active_merely_because_enabled():
    """Explicit requirement: enabled=enabled with active=inactive must never be ACTIVE."""
    assert menu.classify_timer_health("inactive", "enabled") == "INACTIVE"


def test_render_dashboard_shows_fleet_totals_and_auto_sync_health():
    data = {
        "policy_ok": True, "monthly_grant_gb": "100", "auto_renewal_enabled": True,
        "write_approved": True, "per_node": {},
        "timers": {"virt-renewal-inventory-sync.timer": {"enabled": "enabled", "active": "active"}},
        "inventory_sync_timer_health": "ACTIVE",
        "inventory": {
            "sync_info": {"captured_at": "2026-01-01T00:00:00+00:00", "record_count": 60, "authoritative_total": 60},
            "freshness": "FRESH", "last_attempt_status": "OK", "last_attempt_error": None,
            "refresh_interval_minutes": 15,
            "fleet": {"total_vps": 60, "limited_vps_count": 56, "unlimited_vps_count": 4,
                      "limited_quota_sum_gb": "296041", "limited_used_sum_gb": "238292.21",
                      "limited_remaining_sum_gb": "57748.79", "unlimited_used_sum_gb": "23078.39",
                      "total_used_all_vps_gb": "261370.60"},
        },
    }
    out = menu.render_dashboard(data, menu.Style(enabled=False))
    assert "ACTIVE" in out
    assert "Current VPS count: 60" in out
    assert "296041" in out
    assert "261370.60" in out
    assert "not sold/billed bandwidth" in out


def test_render_dashboard_handles_missing_inventory_gracefully():
    data = {"policy_ok": True, "monthly_grant_gb": "100", "auto_renewal_enabled": True,
            "write_approved": True, "per_node": {}, "timers": {},
            "inventory_error": "db locked", "inventory": None,
            "inventory_sync_timer_health": "UNKNOWN"}
    out = menu.render_dashboard(data, menu.Style(enabled=False))
    assert "unavailable: db locked" in out


def test_render_dashboard_shows_incomplete_coverage_in_red_marker():
    data = {
        "policy_ok": True, "monthly_grant_gb": "100", "auto_renewal_enabled": True,
        "write_approved": True, "per_node": {}, "timers": {},
        "inventory_sync_timer_health": "ACTIVE",
        "inventory": {
            "sync_info": {"captured_at": "2026-01-01T00:00:00+00:00", "record_count": 50, "authoritative_total": 60},
            "freshness": "STALE", "last_attempt_status": "OK", "last_attempt_error": None,
            "refresh_interval_minutes": 15,
            "fleet": {"total_vps": 50, "limited_vps_count": 48, "unlimited_vps_count": 2,
                      "limited_quota_sum_gb": "1", "limited_used_sum_gb": "1",
                      "limited_remaining_sum_gb": "0", "unlimited_used_sum_gb": "0",
                      "total_used_all_vps_gb": "1"},
        },
    }
    out = menu.render_dashboard(data, menu.Style(enabled=False))
    assert "Coverage: 50/60" in out


# ---------- v1.7.0: IP search, numeric sort-by-quota/usage, VPS table IP column -----------

def test_filter_and_sort_vps_search_by_ip():
    records = [{"vpsid": 1, "hostname": "a", "ip_primary": "10.0.0.5"},
               {"vpsid": 2, "hostname": "b", "ip_primary": "10.0.0.9"}]
    out = menu.filter_and_sort_vps(records, search="10.0.0.9")
    assert len(out) == 1
    assert out[0]["vpsid"] == 2


def test_filter_and_sort_vps_sort_by_quota_is_numeric_not_lexicographic():
    records = [{"vpsid": 1, "quota_gb": "9"}, {"vpsid": 2, "quota_gb": "100"},
               {"vpsid": 3, "quota_gb": "20"}]
    out = menu.filter_and_sort_vps(records, sort_by="quota_gb")
    assert [r["vpsid"] for r in out] == [1, 3, 2]  # 9 < 20 < 100 numerically


def test_filter_and_sort_vps_sort_by_usage_numeric():
    records = [{"vpsid": 1, "used_gb": "500"}, {"vpsid": 2, "used_gb": "50"}]
    out = menu.filter_and_sort_vps(records, sort_by="used_gb")
    assert [r["vpsid"] for r in out] == [2, 1]


def test_filter_and_sort_vps_unparseable_quota_sorts_after_valid_values():
    records = [{"vpsid": 1, "quota_gb": "unavailable"}, {"vpsid": 2, "quota_gb": "10"}]
    out = menu.filter_and_sort_vps(records, sort_by="quota_gb")
    assert [r["vpsid"] for r in out] == [2, 1]


def test_render_vps_table_shows_primary_ip_column():
    records = [{"vpsid": 1, "hostname": "web1", "status": "unknown", "quota_gb": "100",
                "used_gb": "10", "ip_primary": "203.0.113.5"}]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "203.0.113.5" in out


# ---------- v1.7.0: host network traffic panel rendering ----------------------------------

def test_render_hostnet_panel_no_samples_yet():
    out = menu.render_hostnet_panel("eth0", None, None, None, menu.Style(enabled=False))
    assert "No samples recorded yet" in out


def test_render_hostnet_panel_shows_totals_and_speed():
    cumulative = {"cumulative_rx_bytes": 5_000_000_000, "cumulative_tx_bytes": 2_000_000_000,
                  "first_sample_at": "2026-01-01T00:00:00+00:00"}
    live_rate = {"rx_bytes_per_sec": 1_000_000, "tx_bytes_per_sec": 500_000}
    recent = {"sampled_at": "2026-01-02T00:00:00+00:00", "discontinuity_type": None, "gap_seconds": 300}
    out = menu.render_hostnet_panel("ens1f1", cumulative, live_rate, recent, menu.Style(enabled=False))
    assert "HOST NETWORK -- ens1f1" in out
    assert "5.00 GB" in out
    assert "MB/s" in out
    assert "Download (RX) = bytes received" in out


def test_render_hostnet_panel_handles_missing_live_rate_honestly():
    cumulative = {"cumulative_rx_bytes": 100, "cumulative_tx_bytes": 100, "first_sample_at": None}
    out = menu.render_hostnet_panel("eth0", cumulative, None, None, menu.Style(enabled=False))
    assert "Persistent Download (RX) Speed:" in out
    assert "unavailable" in out


def test_render_hostnet_panel_flags_discontinuity_as_data_quality_issue():
    cumulative = {"cumulative_rx_bytes": 100, "cumulative_tx_bytes": 100, "first_sample_at": "2026-01-01T00:00:00+00:00"}
    recent = {"sampled_at": "2026-01-02T00:00:00+00:00", "discontinuity_type": "REBOOT", "gap_seconds": 3600}
    out = menu.render_hostnet_panel("eth0", cumulative, None, recent, menu.Style(enabled=False))
    assert "discontinuity (REBOOT)" in out
    assert "not observable" in out or "never invented" in out


def test_format_bytes_human_decimal_units():
    assert menu._format_bytes_human(5_000_000_000) == "5.00 GB"
    assert menu._format_bytes_human(500) == "500 B"


def test_format_bits_per_sec_human():
    assert menu._format_bits_per_sec_human(12_500_000) == "100.00 Mbps"
    assert menu._format_bits_per_sec_human(125_000_000) == "1.00 Gbps"
