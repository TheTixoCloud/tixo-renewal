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


def test_format_local_time_converts_utc_to_tehran():
    # 2026-01-01T00:00:00 UTC -> Asia/Tehran is UTC+3:30 -> 03:30 local, same calendar day.
    out = menu.format_local_time("2026-01-01T00:00:00+00:00", "Asia/Tehran")
    assert "03:30:00" in out
    assert "2026-01-01" in out


def test_format_local_time_handles_naive_timestamp_as_utc():
    out = menu.format_local_time("2026-01-01T00:00:00", "UTC")
    assert "2026-01-01 00:00:00" in out


def test_format_local_time_never_raises_on_garbage_falls_back_to_raw():
    out = menu.format_local_time("not-a-timestamp", "Asia/Tehran")
    assert "not-a-timestamp" in out


def test_format_local_time_timezone_boundary_crosses_calendar_day():
    """A known tricky case: a UTC time late in the day crosses into the NEXT calendar day in
    a positive-offset timezone -- must be handled correctly, not just offset-blind."""
    out = menu.format_local_time("2026-06-30T21:00:00+00:00", "Asia/Tehran")
    assert "2026-07-01" in out  # 21:00 UTC + 3:30 = 00:30 the next day
    assert "00:30:00" in out


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
