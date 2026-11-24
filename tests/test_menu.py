"""Tests for the interactive management menu's pure/testable logic. The input()-driven REPL
loop itself is thin glue over these functions and over existing, already-tested renewal_cli.py
commands -- not re-tested here; what matters is the logic that validates operator input and
assembles data before any of it reaches a subprocess call or a template.
"""
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import menu
import policy as pol
import installer


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


def test_action_configure_gift_writes_validated_value_to_policy_json(tmp_path, monkeypatch, capsys):
    """action_configure_gift() itself (the input()->policy.json write glue, as opposed to
    parse_gift_gb()'s already-tested validation) had zero test coverage until this round's
    final audit -- this is a direct financial business rule (the monthly renewal gift), not
    cosmetic menu glue."""
    import policy as pol_mod
    menu.renewal_cli.CONFIG_DIR = str(tmp_path)
    policy_path = tmp_path / "policy.json"
    pol_mod.write_default_policy_config(str(policy_path))
    monkeypatch.setattr("builtins.input", lambda *a: "250")
    menu.action_configure_gift()
    cfg = pol_mod.load_policy_config(str(policy_path))
    assert cfg.global_defaults.monthly_grant_gb == Decimal("250")
    assert "250" in capsys.readouterr().out


def test_action_configure_gift_rejects_invalid_input_without_writing(tmp_path, monkeypatch, capsys):
    import policy as pol_mod
    menu.renewal_cli.CONFIG_DIR = str(tmp_path)
    policy_path = tmp_path / "policy.json"
    pol_mod.write_default_policy_config(str(policy_path))
    before = policy_path.read_text()
    monkeypatch.setattr("builtins.input", lambda *a: "-5")
    menu.action_configure_gift()
    assert policy_path.read_text() == before  # untouched on rejection
    assert "Rejected" in capsys.readouterr().out


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


# ---------- v1.10.2: action_check_updates -- real offer-to-apply flow --------------------

def _fake_check_response(tag, monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"tag_name": tag, "html_url": "https://example.invalid"}).encode()
    monkeypatch.setattr(menu.urllib.request, "urlopen", lambda req, timeout=5: FakeResp())


def test_action_check_updates_no_update_available_never_prompts(monkeypatch, capsys):
    _fake_check_response("v1.1.0", monkeypatch)
    menu.renewal_cli.VERSION = "1.1.0"
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(
        AssertionError("must never prompt when already up to date")))
    menu.action_check_updates(menu.Style(enabled=False))
    out = capsys.readouterr().out
    assert "Already up to date" in out


def test_action_check_updates_declined_never_applies(monkeypatch, capsys):
    _fake_check_response("v9.9.9", monkeypatch)
    menu.renewal_cli.VERSION = "1.1.0"
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    called = {"apply": False}
    monkeypatch.setattr(menu.renewal_cli, "cmd_update_apply",
                         lambda args: called.__setitem__("apply", True) or 0)
    menu.action_check_updates(menu.Style(enabled=False))
    assert called["apply"] is False
    out = capsys.readouterr().out
    assert "Not applying" in out


def test_action_check_updates_confirmed_success_reexecs_the_process(monkeypatch, capsys):
    """The success path must re-exec a fresh process (never just 'return' and keep running
    the OLD in-memory code) -- this test replaces os.execv with a recorder instead of a
    real process replacement, which would terminate the test runner itself."""
    _fake_check_response("v9.9.9", monkeypatch)
    menu.renewal_cli.VERSION = "1.1.0"
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(menu.renewal_cli, "cmd_update_apply", lambda args: 0)
    execv_calls = []
    monkeypatch.setattr(menu.os, "execv", lambda *a: execv_calls.append(a))
    menu.action_check_updates(menu.Style(enabled=False))
    assert len(execv_calls) == 1
    out = capsys.readouterr().out
    assert "Update applied successfully" in out


def test_action_check_updates_failed_apply_never_execs_and_reports_clearly(monkeypatch, capsys):
    _fake_check_response("v9.9.9", monkeypatch)
    menu.renewal_cli.VERSION = "1.1.0"
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(menu.renewal_cli, "cmd_update_apply", lambda args: 1)
    execv_calls = []
    monkeypatch.setattr(menu.os, "execv", lambda *a: execv_calls.append(a))
    menu.action_check_updates(menu.Style(enabled=False))
    assert execv_calls == []  # never re-execs on a failed update
    out = capsys.readouterr().out
    assert "Update failed" in out
    assert "previous install remains in place" in out


def test_action_check_updates_apply_called_with_yes_to_avoid_double_confirmation(monkeypatch, capsys):
    """action_check_updates already confirms via its own _confirm() prompt -- it must pass
    yes=True through to cmd_update_apply so the operator is never asked to confirm twice."""
    _fake_check_response("v9.9.9", monkeypatch)
    menu.renewal_cli.VERSION = "1.1.0"
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    captured_args = {}
    def fake_apply(args):
        captured_args["args"] = args
        return 0
    monkeypatch.setattr(menu.renewal_cli, "cmd_update_apply", fake_apply)
    monkeypatch.setattr(menu.os, "execv", lambda *a: None)
    menu.action_check_updates(menu.Style(enabled=False))
    assert captured_args["args"].yes is True


# ---------- action_toggle_auto_renewal (previously untested at the menu layer) ------------------
# The single most consequential switch in the whole product -- never tested directly at the
# menu layer before this round's final audit, including the v1.10.5 legacy-write-conflict
# confirmation branch, which had ZERO test coverage anywhere until now.

def _no_legacy_conflict(monkeypatch):
    monkeypatch.setattr(installer, "detect_legacy_bandwidth_automation",
                         lambda: {"found_active": [], "found_disabled": [],
                                  "active_write_capable": False})


def _legacy_write_conflict(monkeypatch):
    monkeypatch.setattr(installer, "detect_legacy_bandwidth_automation",
                         lambda: {"found_active": ["virt-carryover-auto"], "found_disabled": [],
                                  "active_write_capable": True})


def test_action_toggle_auto_renewal_enable_declined_never_calls_enable(monkeypatch, capsys):
    _no_legacy_conflict(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    called = []
    monkeypatch.setattr(installer, "enable_auto_renewal", lambda *a, **k: called.append(1))
    menu.action_toggle_auto_renewal(True)
    assert called == []
    assert "Cancelled" in capsys.readouterr().out


def test_action_toggle_auto_renewal_enable_confirmed_no_legacy_conflict(monkeypatch, capsys):
    _no_legacy_conflict(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    captured = {}
    def fake_enable(config_dir, systemd_dir, override_legacy_write_conflict=False):
        captured["override"] = override_legacy_write_conflict
        return {"ok": True, "steps": ["did it"], "problems": []}
    monkeypatch.setattr(installer, "enable_auto_renewal", fake_enable)
    menu.action_toggle_auto_renewal(True, menu.Style(enabled=False))
    assert captured["override"] is False
    assert "did it" in capsys.readouterr().out


def test_action_toggle_auto_renewal_enable_legacy_conflict_declined_at_risk_ack_never_enables(monkeypatch, capsys):
    """The FIRST confirmation (acknowledging the specific double-write risk) must be
    answered separately from the second (the general enable confirmation) -- declining the
    first must cancel immediately, never falling through to ask the second."""
    _legacy_write_conflict(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    called = []
    monkeypatch.setattr(installer, "enable_auto_renewal", lambda *a, **k: called.append(1))
    menu.action_toggle_auto_renewal(True, menu.Style(enabled=False))
    assert called == []
    out = capsys.readouterr().out
    assert "Cancelled" in out
    assert "virt-carryover-auto" in out


def test_action_toggle_auto_renewal_enable_legacy_conflict_override_confirmed_passes_override_true(monkeypatch, capsys):
    """Both the risk-acknowledgement AND the final enable confirmation must be answered 'y'
    before override_legacy_write_conflict=True is ever passed through."""
    _legacy_write_conflict(monkeypatch)
    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    captured = {}
    def fake_enable(config_dir, systemd_dir, override_legacy_write_conflict=False):
        captured["override"] = override_legacy_write_conflict
        return {"ok": True, "steps": [], "problems": []}
    monkeypatch.setattr(installer, "enable_auto_renewal", fake_enable)
    menu.action_toggle_auto_renewal(True, menu.Style(enabled=False))
    assert captured["override"] is True


def test_action_toggle_auto_renewal_enable_legacy_conflict_ack_yes_then_final_no_never_enables(monkeypatch, capsys):
    """Acknowledging the risk but then declining the FINAL confirmation must still cancel --
    the risk acknowledgement alone is never sufficient to enable anything."""
    _legacy_write_conflict(monkeypatch)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    called = []
    monkeypatch.setattr(installer, "enable_auto_renewal", lambda *a, **k: called.append(1))
    menu.action_toggle_auto_renewal(True, menu.Style(enabled=False))
    assert called == []
    assert "Cancelled" in capsys.readouterr().out


def test_action_toggle_auto_renewal_disable_declined_never_calls_disable(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    called = []
    monkeypatch.setattr(installer, "disable_auto_renewal", lambda *a, **k: called.append(1))
    menu.action_toggle_auto_renewal(False)
    assert called == []
    assert "Cancelled" in capsys.readouterr().out


def test_action_toggle_auto_renewal_disable_confirmed_calls_disable(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    called = []
    monkeypatch.setattr(installer, "disable_auto_renewal",
                         lambda *a, **k: called.append(1) or {"ok": True, "steps": [], "problems": []})
    menu.action_toggle_auto_renewal(False, menu.Style(enabled=False))
    assert called == [1]


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
    # Default monthly gift is 0 -- see policy.BUILTIN_DEFAULT_MONTHLY_GB's docstring.
    assert data["monthly_grant_gb"] == "0"
    assert "self" in data["nodes"]
    assert data["per_node"]["self"]["initialized"] is False  # no sqlite3 file created yet


def test_collect_dashboard_data_includes_readiness_counts_distinct_from_service_health(tmp_path):
    """v1.10.6: the dashboard must surface financial-engine readiness (COLLECTING_BASELINE/
    MONITORING/REVIEW_REQUIRED) SEPARATELY from service/timer health -- a node can be
    perfectly healthy at the service level while every VPS is still collecting its first
    observations, and that must never be hidden or conflated with a service problem."""
    config_dir = str(tmp_path / "renewal-config")
    state_dir = str(tmp_path / "shadow-monitor-state")
    os.makedirs(config_dir)
    os.makedirs(state_dir)
    pol.write_default_policy_config(os.path.join(config_dir, "policy.json"))
    import nodes as nodes_mod
    nodes_mod.write_default_config(os.path.join(config_dir, "nodes.json"),
                                    nodes=[nodes_mod.NodeConfig("self", mode="local", db_filename="self.sqlite3")])
    import shadow_monitor as sm_mod
    conn = sm_mod.open_db(os.path.join(state_dir, "self.sqlite3"))
    sm_mod.ingest_poll(conn, [{"vpsid": 1, "created_epoch": 1700000000,
                               "quota_gb": Decimal("100"), "used_gb": Decimal("10")}],
                        sm_mod.utcnow_iso())
    conn.close()
    data = menu.collect_dashboard_data(config_dir, state_dir)
    assert data["per_node"]["self"]["readiness_counts"] == {"COLLECTING_BASELINE": 1}


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


def _advanced_view_info():
    return {
        "os": {"hostname": "h", "distribution": "d", "kernel": "k", "architecture": "a"},
        "cpu": {"vendor": "v", "model": "m", "physical_cores": 1, "logical_cpus": 2, "threads_per_core": 2},
        "memory": {"total_mb": 100, "available_mb": 50, "swap_total_mb": 10, "swap_free_mb": 5},
        "disks": [],
        "uptime": {"uptime_seconds": 3600, "load_average": {"1m": 1, "5m": 1, "15m": 1}},
        "usage": {"cpu_percent": "unsupported", "memory_percent": 50.0},
        "temperature": {"available": False, "reason": "unavailable", "zones": []},
        "network_interfaces": [
            {"name": "ens1f1", "state": "up", "ipv4": "1.2.3.4", "is_virtual": False},
            {"name": "viifbr0", "state": "up", "ipv4": "1.2.3.5", "is_virtual": False},
            {"name": "viifv1001", "state": "up", "ipv4": "unavailable", "is_virtual": True},
            {"name": "viifv1002", "state": "up", "ipv4": "unavailable", "is_virtual": True},
        ],
        "host_timezone": "UTC",
    }


def test_render_host_info_advanced_view_category_filter_physical():
    out = menu.render_host_info(_advanced_view_info(), menu.Style(enabled=False),
                                 show_virtual_interfaces=True, category_filter="physical")
    assert "ens1f1" in out
    assert "viifbr0" not in out
    assert "viifv1001" not in out


def test_render_host_info_advanced_view_category_filter_bridge():
    out = menu.render_host_info(_advanced_view_info(), menu.Style(enabled=False),
                                 show_virtual_interfaces=True, category_filter="bridge")
    assert "viifbr0" in out
    assert "ens1f1" not in out
    assert "viifv1001" not in out


def test_render_host_info_advanced_view_search_term():
    out = menu.render_host_info(_advanced_view_info(), menu.Style(enabled=False),
                                 show_virtual_interfaces=True, search_term="viifv")
    assert "viifv1001" in out
    assert "viifv1002" in out
    assert "ens1f1" not in out
    assert "viifbr0" not in out


def test_render_host_info_advanced_view_unfiltered_shows_everything():
    out = menu.render_host_info(_advanced_view_info(), menu.Style(enabled=False),
                                 show_virtual_interfaces=True)
    for name in ("ens1f1", "viifbr0", "viifv1001", "viifv1002"):
        assert name in out


def test_render_host_info_compact_view_always_shows_bridge_and_physical():
    """The host bridge and physical interfaces must appear in the DEFAULT (compact) view
    too -- only per-VM virtual interfaces are hidden by default."""
    out = menu.render_host_info(_advanced_view_info(), menu.Style(enabled=False))
    assert "ens1f1" in out
    assert "viifbr0" in out
    assert "viifv1001" not in out
    assert "viifv1002" not in out


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


# ---------- v1.8.0: adaptive VPS table tiers, truncation, Unicode width -----------------------

def _vps_record(**overrides):
    base = {"vpsid": 1, "hostname": "web1", "status": "running", "runtime_status": "running",
            "quota_gb": "100", "used_gb": "10", "ip_primary": "203.0.113.5"}
    base.update(overrides)
    return base


def test_render_vps_table_medium_tier_hides_ip_shows_used_and_limit_columns(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((80, 24)))
    records = [_vps_record(used_gb="10", quota_gb="100")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "web1" in out
    assert "10.00 GB" in out  # Used
    assert "100.00 GB" in out  # Limit
    assert "203.0.113.5" not in out


def test_render_vps_table_narrow_tier_shows_only_id_hostname_usage(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((60, 24)))
    records = [_vps_record(used_gb="25", quota_gb="100")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "web1" in out
    assert "25%" in out
    assert "203.0.113.5" not in out
    assert "10/100" not in out  # medium-tier-only format never leaks into narrow


def test_render_vps_table_medium_tier_marks_unlimited_distinct_from_zero_usage():
    import shutil
    orig = shutil.get_terminal_size
    try:
        shutil.get_terminal_size = lambda fallback=None: __import__("os").terminal_size((80, 24))
        unlimited = _vps_record(vpsid=1, hostname="unlimited-vps", quota_gb="0", used_gb="500")
        zero_usage = _vps_record(vpsid=2, hostname="idle-vps", quota_gb="100", used_gb="0")
        out = menu.render_vps_table([unlimited, zero_usage], menu.Style(enabled=False))
        assert "UNLIMITED" in out
        assert "0.00 GB" in out  # zero usage shown as a real 0, never confused with unlimited
        assert "100.00 GB" in out  # idle-vps's limit
    finally:
        shutil.get_terminal_size = orig


def test_render_vps_table_truncates_long_hostname_with_ellipsis(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((60, 24)))
    long_name = "a-very-long-hostname-that-does-not-fit"
    records = [_vps_record(hostname=long_name)]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert long_name not in out
    assert "…" in out


def test_render_vps_table_short_hostname_never_gets_an_ellipsis():
    records = [_vps_record(hostname="web1")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "…" not in out


def test_display_width_helpers_handle_wide_unicode_characters():
    # CJK characters occupy two terminal columns each.
    assert menu._display_width("日本語") == 6
    assert menu._display_width("abc") == 3


def test_truncate_display_respects_wide_character_width():
    truncated = menu._truncate_display("日本語abcdef", 7)
    assert menu._display_width(truncated) <= 7
    assert truncated.endswith("…")


def test_truncate_display_leaves_fitting_string_unchanged():
    assert menu._truncate_display("short", 20) == "short"


def test_pad_display_accounts_for_wide_characters():
    padded = menu._pad_display("日本", 10)
    assert menu._display_width(padded) == 10


def test_render_vps_table_unicode_hostname_does_not_crash_narrow_tier(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((60, 24)))
    records = [_vps_record(hostname="日本語サーバー001")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert out  # must render without raising, regardless of exact truncation point


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


# ---------- v1.8.0: exhausted-bandwidth policy menu wiring -----------------------------------

def test_menu_items_includes_exhausted_bandwidth_policy_screens():
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Configure exhausted-bandwidth policy" in labels
    assert "Check exhausted VPS bandwidth" in labels
    assert "Manual review queue" in labels
    assert "Emergency grants (proposed)" in labels


def test_action_check_exhausted_bandwidth_noop_when_policy_disabled(tmp_path, capsys):
    menu.renewal_cli.CONFIG_DIR = str(tmp_path)
    menu.renewal_cli.STATE_DIR = str(tmp_path)
    menu.action_check_exhausted_bandwidth(menu.Style(enabled=False))
    out = capsys.readouterr().out
    assert "disabled" in out.lower()


def test_action_view_manual_review_queue_empty_state(tmp_path, capsys):
    menu.renewal_cli.STATE_DIR = str(tmp_path)
    menu.action_view_manual_review_queue(menu.Style(enabled=False))
    out = capsys.readouterr().out
    assert "No pending manual-review items" in out


def test_action_view_emergency_grants_empty_state(tmp_path, capsys):
    menu.renewal_cli.STATE_DIR = str(tmp_path)
    menu.action_view_emergency_grants(menu.Style(enabled=False))
    out = capsys.readouterr().out
    assert "No proposed emergency grants" in out


def test_action_view_emergency_grants_blocks_apply_without_approval_marker(tmp_path, capsys, monkeypatch):
    import emergency_policy as emp
    menu.renewal_cli.STATE_DIR = str(tmp_path)
    menu.renewal_cli.CONFIG_DIR = str(tmp_path)
    conn = emp.open_db(os.path.join(str(tmp_path), "emergency_policy.sqlite3"))
    policy = emp.ExhaustedBandwidthPolicy(mode=emp.MODE_EMERGENCY_GRANT)
    emp.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="150", policy=policy)
    conn.close()
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError(
        "must never prompt for anything once blocked by missing approval marker")))
    menu.action_view_emergency_grants(menu.Style(enabled=False))
    out = capsys.readouterr().out
    assert "blocked" in out.lower()
    assert "EMERGENCY_GRANTS_APPROVED" in out


def test_menu_items_includes_telegram_bot_config():
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Configure Telegram bot (optional)" in labels


def test_menu_items_includes_relay_worker_config():
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Configure Relay Worker (optional, run on a SEPARATE host)" in labels


# ---------- CLI option 4: Network Interfaces explorer (v1.9.0) --------------------------------

def _fake_interfaces():
    ifaces = [
        {"name": "ens1f1", "state": "up", "ipv4": "87.107.168.129", "ipv6": "unavailable",
         "is_virtual": False, "is_uninformative": False, "rx_bytes": 1_000_000_000, "tx_bytes": 500_000_000},
        {"name": "viifbr0", "state": "up", "ipv4": "10.0.0.1", "ipv6": "unavailable",
         "is_virtual": False, "is_uninformative": False, "rx_bytes": 2_000_000_000, "tx_bytes": 1_000_000_000},
        {"name": "lo", "state": "up", "ipv4": "127.0.0.1", "ipv6": "unavailable",
         "is_virtual": False, "is_uninformative": True, "rx_bytes": 0, "tx_bytes": 0},
    ]
    for i in range(60):
        ifaces.append({"name": f"viifv{i}", "state": "up", "ipv4": "unavailable", "ipv6": "unavailable",
                        "is_virtual": True, "is_uninformative": False, "rx_bytes": 123, "tx_bytes": 456})
    return ifaces


def _run_interfaces_menu(monkeypatch, capsys, inputs):
    monkeypatch.setattr(menu.hostinfo, "collect_host_info",
                         lambda: {"network_interfaces": _fake_interfaces()})
    monkeypatch.setattr(menu, "clear_screen", lambda: None)
    it = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))
    menu.action_system_info_advanced(menu.Style(enabled=False))
    return capsys.readouterr().out


def test_default_shown_interfaces_excludes_virtual_and_uninformative():
    shown = menu.default_shown_interfaces(_fake_interfaces())
    names = {i["name"] for i in shown}
    assert names == {"ens1f1", "viifbr0"}


def test_option4_enter_defaults_to_main_shows_only_physical_and_bridge(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["", "", "7"])
    assert "ens1f1" in out
    assert "viifbr0" in out
    assert "viifv0" not in out, "Main must never dump the 60+ per-VM virtual interfaces"


def test_option4_all_shows_every_interface(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["2", "", "7"])
    assert "viifv0" in out and "viifv59" in out and "ens1f1" in out


def test_option4_physical_only(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["3", "", "7"])
    assert "ens1f1" in out
    assert "viifbr0" not in out
    assert "viifv0" not in out


def test_option4_bridges_only(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["4", "", "7"])
    assert "viifbr0" in out
    assert "ens1f1" not in out


def test_option4_virtual_only(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["5", "", "7"])
    assert "viifv0" in out
    assert "ens1f1" not in out
    assert "viifbr0" not in out


def test_option4_search_by_name(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["6", "viifv3", "", "7"])
    assert "viifv3" in out


def test_option4_back_exits_without_error(monkeypatch, capsys):
    out = _run_interfaces_menu(monkeypatch, capsys, ["7"])
    assert "Network Interfaces" in out


def test_option4_invalid_choice_reprompts_instead_of_crashing(monkeypatch, capsys):
    """An invalid choice must re-show the submenu (not crash, not silently fall through to
    any specific view) -- confirmed here by the submenu header appearing twice in the
    transcript (once for the invalid attempt, once after re-looping)."""
    out = _run_interfaces_menu(monkeypatch, capsys, ["99", "", "7"])
    assert out.count("=== Network Interfaces ===") == 2


def test_option4_never_mutates_interface_list(monkeypatch, capsys):
    """Display filtering only -- action_system_info_advanced must never alter the underlying
    interface data it was given."""
    ifaces = _fake_interfaces()
    snapshot = [dict(i) for i in ifaces]
    monkeypatch.setattr(menu.hostinfo, "collect_host_info", lambda: {"network_interfaces": ifaces})
    monkeypatch.setattr(menu, "clear_screen", lambda: None)
    it = iter(["2", "", "3", "", "5", "", "7"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))
    menu.action_system_info_advanced(menu.Style(enabled=False))
    assert ifaces == snapshot


# ---------- CLI option 5: exact column order (v1.9.0) ------------------------------------------

def test_render_vps_table_wide_tier_header_matches_exact_spec_column_order(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((120, 24)))
    records = [_vps_record()]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    header_line = out.splitlines()[0]
    # exact required order: VPS ID | Name | IP | Status | Used | Limit | Usage %
    for col in ("VPS ID", "Name", "IP", "Status", "Used", "Limit", "Usage %"):
        assert col in header_line
    assert header_line.index("VPS ID") < header_line.index("Name") < header_line.index("IP")
    assert header_line.index("IP") < header_line.index("Status") < header_line.index("Used")
    assert header_line.index("Used") < header_line.index("Limit") < header_line.index("Usage %")


def test_render_vps_table_wide_tier_shows_used_before_limit_in_data_row(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((120, 24)))
    records = [_vps_record(used_gb="10", quota_gb="100")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    data_line = out.splitlines()[1]
    assert data_line.index("10.00 GB") < data_line.index("100.00 GB")


def test_render_vps_table_wide_tier_unlimited_shows_textual_limit_not_bytes(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((120, 24)))
    records = [_vps_record(quota_gb="0", used_gb="500")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    assert "UNLIMITED" in out
    assert "500.00 GB" in out  # actual usage still shown, never hidden just because unlimited


def test_render_vps_table_progress_bar_stays_compact(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                         lambda fallback=None: os.terminal_size((120, 24)))
    records = [_vps_record(used_gb="50", quota_gb="100")]
    out = menu.render_vps_table(records, menu.Style(enabled=False))
    bar = menu.render_progress_bar(50.0, width=8, style=menu.Style(enabled=False))
    assert bar in out
    assert out.count("50.0%") == 1  # never duplicated -- the bar's own embedded percent is the only one
    assert menu._display_width(bar) <= 20  # "[########] 100.0%" worst case -- compact, not an oversized widget


# ---------- v1.10.2: action_uninstall -- real two-mode flow + clean process exit -----------

def _fake_preview(purge, **overrides):
    base = {
        "mode": "full_purge" if purge else "standard",
        "install_dir_exists": True,
        "will_stop_disable_units": [],
        "will_remove_unit_files": [],
        "will_remove_global_wrapper": True,
        "will_remove_entries": ["lib", "bin", "systemd"] + (["renewal-config", "shadow-monitor-state"] if purge else []),
        "will_preserve_entries": ["backups"] + ([] if purge else ["renewal-config", "shadow-monitor-state"]),
        "unknown_entries_never_touched": [],
    }
    base.update(overrides)
    return base


def test_action_uninstall_cancelled_at_mode_choice_never_calls_uninstall(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "9")  # invalid mode choice
    called = []
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", lambda args: called.append(args) or 0)
    with pytest.raises(SystemExit) as exc_info:
        # Should return normally (no SystemExit) if truly cancelled -- assert it does NOT exit.
        menu.action_uninstall(menu.Style(enabled=False))
        raise SystemExit(99)  # sentinel: reached here means action_uninstall returned normally
    assert exc_info.value.code == 99
    assert called == []


def test_action_uninstall_standard_wrong_confirmation_cancels(monkeypatch, capsys):
    inputs = iter(["1", "not uninstall"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", lambda args: (_ for _ in ()).throw(
        AssertionError("must never call cmd_uninstall when confirmation text is wrong")))
    monkeypatch.setattr(menu.un, "preview_uninstall", lambda root, purge: _fake_preview(purge))
    with pytest.raises(SystemExit) as exc_info:
        menu.action_uninstall(menu.Style(enabled=False))
        raise SystemExit(99)
    assert exc_info.value.code == 99
    out = capsys.readouterr().out
    assert "Cancelled" in out


def test_action_uninstall_standard_confirmed_calls_cmd_uninstall_with_yes_and_exits(monkeypatch, capsys):
    inputs = iter(["1", "UNINSTALL", "n"])  # mode, confirm, decline backup deletion
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(menu.un, "preview_uninstall", lambda root, purge: _fake_preview(purge))
    captured = {}
    def fake_uninstall(args):
        captured["args"] = args
        return 0
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", fake_uninstall)
    with pytest.raises(SystemExit) as exc_info:
        menu.action_uninstall(menu.Style(enabled=False))
    assert exc_info.value.code == 0
    assert captured["args"].purge is False
    assert captured["args"].yes is True
    assert captured["args"].delete_backups is False
    out = capsys.readouterr().out
    assert "Uninstall complete" in out


def test_action_uninstall_purge_requires_exact_phrase(monkeypatch, capsys):
    inputs = iter(["2", "purge everything please"])  # mode=purge, wrong phrase
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(menu.un, "preview_uninstall", lambda root, purge: _fake_preview(purge))
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", lambda args: (_ for _ in ()).throw(
        AssertionError("must never purge without the exact confirmation phrase")))
    with pytest.raises(SystemExit) as exc_info:
        menu.action_uninstall(menu.Style(enabled=False))
        raise SystemExit(99)
    assert exc_info.value.code == 99


def test_action_uninstall_purge_confirmed_passes_correct_confirm_phrase(monkeypatch, capsys):
    inputs = iter(["2", menu.renewal_cli.REQUIRED_PURGE_PHRASE, "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(menu.un, "preview_uninstall", lambda root, purge: _fake_preview(purge))
    captured = {}
    def fake_uninstall(args):
        captured["args"] = args
        return 0
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", fake_uninstall)
    with pytest.raises(SystemExit) as exc_info:
        menu.action_uninstall(menu.Style(enabled=False))
    assert exc_info.value.code == 0
    assert captured["args"].purge is True
    assert captured["args"].confirm == menu.renewal_cli.REQUIRED_PURGE_PHRASE


def test_action_uninstall_failed_uninstall_exits_nonzero(monkeypatch, capsys):
    inputs = iter(["1", "UNINSTALL", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(menu.un, "preview_uninstall", lambda root, purge: _fake_preview(purge))
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", lambda args: 1)
    with pytest.raises(SystemExit) as exc_info:
        menu.action_uninstall(menu.Style(enabled=False))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "problems" in out.lower()


def test_action_uninstall_unknown_entries_shown_in_preview(monkeypatch, capsys):
    inputs = iter(["1", "UNINSTALL", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(menu.un, "preview_uninstall",
                         lambda root, purge: _fake_preview(purge, unknown_entries_never_touched=["mystery.txt"]))
    monkeypatch.setattr(menu.renewal_cli, "cmd_uninstall", lambda args: 0)
    with pytest.raises(SystemExit):
        menu.action_uninstall(menu.Style(enabled=False))
    out = capsys.readouterr().out
    assert "mystery.txt" in out


# ---------- v1.10.10: backup-schedule activation must be REAL, not just a template ----------

def test_action_configure_backup_schedule_enable_actually_activates_real_systemd(
        monkeypatch, capsys, tmp_path):
    """Confirmed usability defect, reported after real operator use (menu option 22): this
    action used to only ever write the systemd unit TEMPLATE and print manual cp/
    daemon-reload/enable instructions -- 'saved' was reported as if it meant 'running',
    which it did not. Fixed (v1.10.10) to reuse service_activation's real
    install_and_enable_units()/verify_units_status(), the same helpers the core timers
    already use. Never touches the real host's /etc/systemd/system -- SYSTEMD_UNIT_DIR is
    monkeypatched to a disposable path, the one safe way to test this (service_activation's
    own module docstring, and _systemctl is the one chokepoint every systemd-aware test in
    this project mocks through)."""
    menu.renewal_cli.CONFIG_DIR = str(tmp_path / "renewal-config")
    menu.renewal_cli.SYSTEMD_OUT_DIR = str(tmp_path / "systemd")
    os.makedirs(menu.renewal_cli.CONFIG_DIR)
    os.makedirs(menu.renewal_cli.SYSTEMD_OUT_DIR)

    import service_activation as sa
    disposable_systemd_dir = tmp_path / "fake-etc-systemd-system"
    disposable_systemd_dir.mkdir()
    monkeypatch.setattr(sa, "SYSTEMD_UNIT_DIR", str(disposable_systemd_dir))
    monkeypatch.setattr(sa, "_systemctl",
                         lambda *a, **k: {"ok": True,
                                           "stdout": "enabled" if a and a[0] == "is-enabled"
                                           else ("active" if a and a[0] == "is-active" else ""),
                                           "stderr": ""})
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    # interval, keep, encrypt?, enable-timer?, save-confirm -- in that exact prompt order.
    answers = iter(["", "", "n", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    menu.action_configure_backup_schedule(menu.Style(enabled=False))

    out = capsys.readouterr().out
    assert "activated" in out.lower()
    # The decisive check: the unit files must have genuinely landed in the (disposable)
    # systemd directory -- a regression back to template-only would never copy them there.
    assert (disposable_systemd_dir / "virt-renewal-backup.service").exists()
    assert (disposable_systemd_dir / "virt-renewal-backup.timer").exists()
    import backup_config as bcfg
    assert bcfg.load_backup_schedule_settings(menu.renewal_cli.CONFIG_DIR).enabled is True


def test_action_configure_backup_schedule_activation_failure_is_reported_not_hidden(
        monkeypatch, capsys, tmp_path):
    """If real activation fails (e.g. systemctl rejects it), this must be reported as a
    failure -- never silently treated as 'saved' success, the exact class of bug this
    whole fix exists to close."""
    menu.renewal_cli.CONFIG_DIR = str(tmp_path / "renewal-config")
    menu.renewal_cli.SYSTEMD_OUT_DIR = str(tmp_path / "systemd")
    os.makedirs(menu.renewal_cli.CONFIG_DIR)
    os.makedirs(menu.renewal_cli.SYSTEMD_OUT_DIR)

    import service_activation as sa
    monkeypatch.setattr(sa, "SYSTEMD_UNIT_DIR", str(tmp_path / "fake-etc-systemd-system"))
    os.makedirs(str(tmp_path / "fake-etc-systemd-system"))
    monkeypatch.setattr(sa, "_systemctl",
                         lambda *a, **k: {"ok": False, "stdout": "", "stderr": "nope"})
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    answers = iter(["", "", "n", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    menu.action_configure_backup_schedule(menu.Style(enabled=False))

    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "NOT actually running" in out


# ---------- v1.10.11: persistent-service modules must ALSO really activate ----------

def test_action_configure_telegram_bot_enable_actually_activates_real_systemd(
        monkeypatch, capsys, tmp_path):
    """Extends the v1.10.10 backup-timer fix to the persistent-service optional modules
    (Telegram bot, Web Control Center, relay worker, remote agent) -- these have a single
    Type=simple/Restart=always .service with NO paired .timer, so the real-activation step
    is `systemctl enable --now` on the service itself (service_activation.
    install_and_enable_persistent_service), not a timer. Never touches the real host's
    /etc/systemd/system -- SYSTEMD_UNIT_DIR is redirected to a disposable path."""
    menu.renewal_cli.CONFIG_DIR = str(tmp_path / "renewal-config")
    menu.renewal_cli.SYSTEMD_OUT_DIR = str(tmp_path / "systemd")
    os.makedirs(menu.renewal_cli.CONFIG_DIR)
    os.makedirs(menu.renewal_cli.SYSTEMD_OUT_DIR)
    # action_configure_telegram_bot writes the unit via renewal_cli.write_systemd_units()
    # normally (through cmd_install) -- here we only need the ONE unit file this test cares
    # about to exist under SYSTEMD_OUT_DIR, for install_and_enable_persistent_service to copy.
    Path(os.path.join(menu.renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-telegram-bot.service")
         ).write_text("[Unit]\nDescription=fake\n[Service]\nExecStart=/bin/true\n")

    import service_activation as sa
    disposable_systemd_dir = tmp_path / "fake-etc-systemd-system"
    disposable_systemd_dir.mkdir()
    monkeypatch.setattr(sa, "SYSTEMD_UNIT_DIR", str(disposable_systemd_dir))
    monkeypatch.setattr(sa, "_systemctl",
                         lambda *a, **k: {"ok": True,
                                           "stdout": "enabled" if a and a[0] == "is-enabled"
                                           else ("active" if a and a[0] == "is-active" else ""),
                                           "stderr": ""})
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    # mode(""), admins(""), viewers(""), enable("y"), reports("n"), set-token("n"),
    # save-confirm("y"), connectivity-test("n") -- in that exact prompt order.
    answers = iter(["", "", "", "y", "n", "n", "y", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    menu.action_configure_telegram_bot(menu.Style(enabled=False))

    out = capsys.readouterr().out
    assert "activated" in out.lower()
    assert (disposable_systemd_dir / "virt-renewal-telegram-bot.service").exists()
    assert menu.tgc.load_telegram_settings(menu.renewal_cli.CONFIG_DIR).enabled is True


def test_action_configure_telegram_bot_activation_failure_is_reported_not_hidden(
        monkeypatch, capsys, tmp_path):
    menu.renewal_cli.CONFIG_DIR = str(tmp_path / "renewal-config")
    menu.renewal_cli.SYSTEMD_OUT_DIR = str(tmp_path / "systemd")
    os.makedirs(menu.renewal_cli.CONFIG_DIR)
    os.makedirs(menu.renewal_cli.SYSTEMD_OUT_DIR)
    Path(os.path.join(menu.renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-telegram-bot.service")
         ).write_text("[Unit]\nDescription=fake\n[Service]\nExecStart=/bin/true\n")

    import service_activation as sa
    monkeypatch.setattr(sa, "SYSTEMD_UNIT_DIR", str(tmp_path / "fake-etc-systemd-system"))
    os.makedirs(str(tmp_path / "fake-etc-systemd-system"))
    monkeypatch.setattr(sa, "_systemctl",
                         lambda *a, **k: {"ok": False, "stdout": "", "stderr": "nope"})
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    answers = iter(["", "", "", "y", "n", "n", "y", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    menu.action_configure_telegram_bot(menu.Style(enabled=False))

    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "NOT actually running" in out
