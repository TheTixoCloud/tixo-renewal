"""Tests for telegram_bot.py (v1.8.0 Part 3): command dispatch, numeric-ID-only
authorization, rate limiting, and transport selection -- all against fake update dicts and
fixture databases, never a real network call.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import telegram_bot as bot
import telegram_config as tc
import security as sec


def _config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    return str(d)


def _state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return str(d)


def _message_update(text, user_id=111, chat_id=999):
    return {"message": {"text": text, "from": {"id": user_id}, "chat": {"id": chat_id}}}


# ---------- update parsing / malformed input ---------------------------------------------------

def test_non_message_update_ignored_cleanly(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update({"edited_message": {}}, settings, _config_dir(tmp_path),
                                 _state_dir(tmp_path))
    assert result["reply"] is None
    assert result["command"] is None


def test_non_command_text_ignored(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("hello there"), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["reply"] is None


def test_command_with_botname_suffix_still_recognized(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/help@MyBot"), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["command"] == "/help"
    assert result["reply"] == bot.HELP_TEXT


# ---------- /start and /help never require authorization ---------------------------------------

def test_start_and_help_work_without_authorization(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[])  # nobody is an admin
    r1 = bot.process_update(_message_update("/start", user_id=999999), settings,
                             _config_dir(tmp_path), _state_dir(tmp_path))
    r2 = bot.process_update(_message_update("/help", user_id=999999), settings,
                             _config_dir(tmp_path), _state_dir(tmp_path))
    assert r1["reply"] == bot.START_TEXT
    assert r2["reply"] == bot.HELP_TEXT
    assert r1["authorized"] is True
    assert r2["authorized"] is True


# ---------- authorization: numeric ID only, never username -------------------------------------

def test_unauthorized_user_blocked_from_status(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/status", user_id=222), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["authorized"] is False
    assert "not authorized" in result["reply"].lower()


def test_admin_id_authorized_for_status(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/status", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["authorized"] is True
    assert "TixoCloud Control Center" in result["reply"]


def test_viewer_id_also_authorized_for_read_only_commands(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[], viewer_ids=[333])
    result = bot.process_update(_message_update("/status", user_id=333), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["authorized"] is True


def test_username_cannot_substitute_for_numeric_id(tmp_path):
    """Confirms the authorization path never even looks at a username field -- only
    message.from.id matters."""
    settings = tc.TelegramSettings(admin_ids=[111])
    update = {"message": {"text": "/status",
                           "from": {"id": 222, "username": "admin"},  # username looks admin-ish
                           "chat": {"id": 1}}}
    result = bot.process_update(update, settings, _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["authorized"] is False


# ---------- rate limiting -----------------------------------------------------------------------

def test_rate_limit_blocks_after_threshold(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(bot.DEFAULT_RATE_LIMIT_MAX_EVENTS):
        bot.process_update(_message_update("/help", user_id=111), settings,
                            _config_dir(tmp_path), _state_dir(tmp_path),
                            rate_limit_conn=conn, now=now)
    blocked = bot.process_update(_message_update("/help", user_id=111), settings,
                                  _config_dir(tmp_path), _state_dir(tmp_path),
                                  rate_limit_conn=conn, now=now)
    assert blocked["rate_limited"] is True
    assert blocked["reply"] is None


def test_rate_limit_is_per_user(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111, 222])
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(bot.DEFAULT_RATE_LIMIT_MAX_EVENTS):
        bot.process_update(_message_update("/help", user_id=111), settings,
                            _config_dir(tmp_path), _state_dir(tmp_path),
                            rate_limit_conn=conn, now=now)
    other_user = bot.process_update(_message_update("/help", user_id=222), settings,
                                     _config_dir(tmp_path), _state_dir(tmp_path),
                                     rate_limit_conn=conn, now=now)
    assert other_user["rate_limited"] is False


# ---------- command handlers against real (empty/fixture) state -------------------------------

def test_status_command_on_empty_state_does_not_crash(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/status", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert result["reply"] is not None
    assert "Error" not in result["reply"]


def test_network_command_reports_unconfigured_honestly(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/network", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "no network interface configured" in result["reply"].lower()


def test_vps_command_reports_no_inventory_honestly(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/vps", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "no vps inventory" in result["reply"].lower()


def test_alerts_command_reports_disabled_mode(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/alerts", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "disabled" in result["reply"].lower()
    assert "no pending manual reviews" in result["reply"].lower()


def test_alerts_command_shows_real_pending_review(tmp_path):
    import emergency_policy as emp
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    policy = emp.ExhaustedBandwidthPolicy(mode=emp.MODE_MANUAL_REVIEW)
    emp.save_policy_settings(config_dir, policy)
    conn = emp.open_db(os.path.join(state_dir, "emergency_policy.sqlite3"))
    emp.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy)
    conn.close()

    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/alerts", user_id=111), settings,
                                 config_dir, state_dir)
    assert "VPS 7" in result["reply"]


def test_reports_command_shows_schedule_and_last_sent_status(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111], report_hour=9, report_minute=30,
                                    report_timezone="UTC")
    result = bot.process_update(_message_update("/reports", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "09:30 UTC" in result["reply"]
    assert "has not sent one yet" in result["reply"]


def test_reports_command_shows_last_sent_date_once_recorded(tmp_path):
    import reports as reports_mod
    settings = tc.TelegramSettings(admin_ids=[111])
    reports_mod.save_last_report_sent_date(_state_dir(tmp_path), "2026-01-01")
    result = bot.process_update(_message_update("/reports", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "2026-01-01" in result["reply"]


def test_backup_command_is_honest_about_not_implemented(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/backup", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "not yet implemented" in result["reply"].lower()


def test_settings_command_never_shows_secrets(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111], connectivity_mode=tc.MODE_DIRECT)
    result = bot.process_update(_message_update("/settings", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "direct" in result["reply"].lower()
    assert "token" not in result["reply"].lower()


def test_unknown_command_reports_cleanly(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/frobnicate", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "unknown command" in result["reply"].lower()


# ---------- transport selection: never silently falls back -------------------------------------

def test_build_transport_direct_mode():
    import telegram_transport as tt
    settings = tc.TelegramSettings(connectivity_mode=tc.MODE_DIRECT)
    secrets = tc.TelegramSecrets(bot_token="t")
    transport = bot.build_transport(settings, secrets)
    assert isinstance(transport, tt.DirectTransport)


def test_build_transport_socks5_mode_requires_host_and_port():
    import pytest
    settings = tc.TelegramSettings(connectivity_mode=tc.MODE_SOCKS5)  # no host/port set
    secrets = tc.TelegramSecrets(bot_token="t")
    with pytest.raises(ValueError, match="not configured"):
        bot.build_transport(settings, secrets)


def test_build_transport_socks5_mode_builds_real_transport_when_configured():
    import telegram_transport as tt
    settings = tc.TelegramSettings(connectivity_mode=tc.MODE_SOCKS5, socks5_host="proxy", socks5_port=1080)
    secrets = tc.TelegramSecrets(bot_token="t", socks5_password="pw")
    transport = bot.build_transport(settings, secrets)
    assert isinstance(transport, tt.Socks5Transport)
    assert transport.proxy.host == "proxy"


def test_build_transport_relay_mode_refuses_direct_construction():
    """Relay mode has no local transport object at all -- the Iran host must never
    accidentally talk directly to Telegram when relay mode is selected."""
    import pytest
    settings = tc.TelegramSettings(connectivity_mode=tc.MODE_RELAY)
    secrets = tc.TelegramSecrets(bot_token="t")
    with pytest.raises(ValueError, match="relay worker"):
        bot.build_transport(settings, secrets)


# ---------- test_transport_connectivity ----------------------------------------------------

def test_transport_connectivity_reports_ok_true_on_success(monkeypatch):
    import telegram_transport as tt
    transport = tt.DirectTransport(bot_token="t")
    monkeypatch.setattr(tt.DirectTransport, "call",
                         lambda self, method, params=None, http_post=None: {"ok": True, "result": {}})
    result = bot.test_transport_connectivity(transport)
    assert result["ok"] is True


def test_transport_connectivity_reports_ok_false_on_transport_error(monkeypatch):
    import telegram_transport as tt
    transport = tt.DirectTransport(bot_token="t")
    def boom(self, method, params=None, http_post=None):
        raise tt.TelegramTransportError("connection failed")
    monkeypatch.setattr(tt.DirectTransport, "call", boom)
    result = bot.test_transport_connectivity(transport)
    assert result["ok"] is False
    assert "connection failed" in result["detail"]


# ---------- run_polling_loop --------------------------------------------------------------------

class _FakeTransport:
    """Records every call made and returns scripted responses -- real network never
    touched. Mirrors the shape of a real transport's .call(method, params) interface."""

    def __init__(self, get_updates_responses):
        self.get_updates_responses = list(get_updates_responses)
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "getUpdates":
            if self.get_updates_responses:
                return self.get_updates_responses.pop(0)
            return {"ok": True, "result": []}
        if method == "sendMessage":
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected method: {method}")


def _update(update_id, text, user_id=111, chat_id=999):
    return {"update_id": update_id,
            "message": {"text": text, "from": {"id": user_id}, "chat": {"id": chat_id}}}


def test_polling_loop_processes_updates_and_advances_offset(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    transport = _FakeTransport([
        {"ok": True, "result": [_update(5, "/help"), _update(6, "/status")]},
    ])
    result = bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                                   max_iterations=1)
    assert result["total_processed"] == 2
    assert result["final_offset"] == 6
    assert bot.load_last_update_id(_state_dir(tmp_path)) == 6


def test_polling_loop_sends_reply_via_transport(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[])
    transport = _FakeTransport([{"ok": True, "result": [_update(1, "/help", chat_id=555)]}])
    bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                          max_iterations=1)
    send_calls = [c for c in transport.calls if c[0] == "sendMessage"]
    assert len(send_calls) == 1
    assert send_calls[0][1]["chat_id"] == 555
    assert send_calls[0][1]["text"] == bot.HELP_TEXT


def test_polling_loop_requests_offset_plus_one_next_time(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    transport = _FakeTransport([
        {"ok": True, "result": [_update(10, "/help")]},
        {"ok": True, "result": []},
    ])
    bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                          max_iterations=2, sleep_fn=lambda s: None)
    get_updates_calls = [c for c in transport.calls if c[0] == "getUpdates"]
    assert get_updates_calls[1][1]["offset"] == 11


def test_polling_loop_resumes_from_persisted_offset_across_restarts(tmp_path):
    state_dir = _state_dir(tmp_path)
    bot.save_last_update_id(state_dir, 42)
    settings = tc.TelegramSettings(admin_ids=[111])
    transport = _FakeTransport([{"ok": True, "result": []}])
    bot.run_polling_loop(transport, settings, _config_dir(tmp_path), state_dir, max_iterations=1)
    assert transport.calls[0][1]["offset"] == 43


def test_polling_loop_survives_transport_error_and_continues(tmp_path):
    import telegram_transport as tt

    class _FlakyTransport(_FakeTransport):
        def call(self, method, params=None):
            if method == "getUpdates" and not self.calls:
                self.calls.append((method, params))
                raise tt.TelegramTransportError("timeout")
            return super().call(method, params)

    settings = tc.TelegramSettings(admin_ids=[111])
    transport = _FlakyTransport([{"ok": True, "result": [_update(1, "/help")]}])
    result = bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                                   max_iterations=2, sleep_fn=lambda s: None)
    assert result["iterations_run"] == 2
    assert result["total_processed"] == 1


def test_polling_loop_reports_errors_via_on_iteration_callback(tmp_path):
    import telegram_transport as tt

    class _AlwaysFailsTransport:
        def call(self, method, params=None):
            raise tt.TelegramTransportError("network down")

    settings = tc.TelegramSettings(admin_ids=[111])
    events = []
    bot.run_polling_loop(_AlwaysFailsTransport(), settings, _config_dir(tmp_path),
                          _state_dir(tmp_path), max_iterations=2,
                          on_iteration=lambda e: events.append(e))
    assert len(events) == 2
    assert all(e["error"] == "network down" for e in events)


def test_polling_loop_does_not_advance_offset_on_empty_batch(tmp_path):
    state_dir = _state_dir(tmp_path)
    bot.save_last_update_id(state_dir, 5)
    settings = tc.TelegramSettings(admin_ids=[111])
    transport = _FakeTransport([{"ok": True, "result": []}])
    bot.run_polling_loop(transport, settings, _config_dir(tmp_path), state_dir, max_iterations=1)
    assert bot.load_last_update_id(state_dir) == 5


def test_load_last_update_id_defaults_to_zero(tmp_path):
    assert bot.load_last_update_id(str(tmp_path)) == 0


def test_save_and_load_last_update_id_roundtrips(tmp_path):
    bot.save_last_update_id(str(tmp_path), 999)
    assert bot.load_last_update_id(str(tmp_path)) == 999


def test_load_last_update_id_malformed_file_falls_back_to_zero(tmp_path):
    path = os.path.join(str(tmp_path), "telegram-offset.json")
    with open(path, "w") as f:
        f.write("{not valid json")
    assert bot.load_last_update_id(str(tmp_path)) == 0


# ---------- renewal_cli.py CLI wiring (argparse registration + cmd_telegram_bot_run) --------

def test_renewal_cli_registers_telegram_bot_run_subcommand():
    """Confirms main()'s argparse wiring: the subcommand parses, dispatches to the right
    handler, and passes through --max-iterations -- without actually running the handler
    (main() always calls sys.exit(), so we intercept the handler itself, letting the real
    parser/dispatch logic run unmodified)."""
    import renewal_cli
    import sys
    import pytest

    captured = {}
    def capture(args):
        captured["args"] = args
        return 0
    orig = renewal_cli.cmd_telegram_bot_run
    renewal_cli.cmd_telegram_bot_run = capture
    old_argv = sys.argv
    sys.argv = ["renewal_cli.py", "telegram-bot-run", "--max-iterations", "3"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            renewal_cli.main()
        assert exc_info.value.code == 0
    finally:
        sys.argv = old_argv
        renewal_cli.cmd_telegram_bot_run = orig

    assert captured["args"].max_iterations == 3


def test_cmd_telegram_bot_run_blocked_when_disabled(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_telegram_bot_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out
    assert "not enabled" in out


def test_cmd_telegram_bot_run_blocked_when_secrets_missing(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    enabled_settings = tc.TelegramSettings(enabled=True, connectivity_mode=tc.MODE_DIRECT)
    tc.save_telegram_settings(renewal_cli.CONFIG_DIR, enabled_settings)

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_telegram_bot_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out
    assert "secrets" in out.lower()


def test_cmd_telegram_bot_run_starts_and_stops_with_max_iterations(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    enabled_settings = tc.TelegramSettings(enabled=True, connectivity_mode=tc.MODE_DIRECT)
    tc.save_telegram_settings(renewal_cli.CONFIG_DIR, enabled_settings)
    tc.store_telegram_secrets(tc.default_secrets_path(renewal_cli.CONFIG_DIR), bot_token="t")

    import telegram_transport as tt
    captured_transport = {}
    def fake_post(url, data, timeout):
        return '{"ok": true, "result": []}'
    orig_direct_call = tt.DirectTransport.call
    def patched_call(self, method, params=None, http_post=None):
        captured_transport["called"] = True
        return orig_direct_call(self, method, params, http_post=fake_post)

    import unittest.mock as mock
    with mock.patch.object(tt.DirectTransport, "call", patched_call):
        class _Args:
            max_iterations = 1
        rc = renewal_cli.cmd_telegram_bot_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert captured_transport.get("called") is True
    assert "Starting Telegram bot polling loop" in out
    assert "Stopped:" in out


# ---------- renewal_cli.py: check-exhausted subcommand (shared with menu's screen) -----------

def test_cmd_check_exhausted_noop_when_disabled(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)

    class _Args:
        node = None
    rc = renewal_cli.cmd_check_exhausted(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "disabled" in out.lower()


def test_cmd_check_exhausted_reports_no_inventory(tmp_path, capsys):
    import renewal_cli
    import emergency_policy as emp
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    emp.save_policy_settings(renewal_cli.CONFIG_DIR,
                              emp.ExhaustedBandwidthPolicy(mode=emp.MODE_MANUAL_REVIEW))

    class _Args:
        node = None
    rc = renewal_cli.cmd_check_exhausted(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "no inventory records" in out.lower()


def test_cmd_check_exhausted_evaluates_real_inventory(tmp_path, capsys):
    import renewal_cli
    import emergency_policy as emp
    import inventory as inv
    from decimal import Decimal

    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    emp.save_policy_settings(renewal_cli.CONFIG_DIR,
                              emp.ExhaustedBandwidthPolicy(mode=emp.MODE_MANUAL_REVIEW))

    conn_inv = inv.open_db(os.path.join(renewal_cli.STATE_DIR, "inventory.sqlite3"))
    parse_result = {
        "complete": True,
        "records": [{"vpsid": 9, "hostname": "h9", "status": "unknown", "quota_gb": Decimal("100"),
                      "used_gb": Decimal("150"), "ram_mb": "1024", "cores": "1", "disk_gb": "20",
                      "virtualization": "kvm", "locked": "unavailable", "node_serid": "unavailable",
                      "ip_primary": "unavailable", "ip_count": "unavailable", "ip_list": "unavailable",
                      "runtime_status": "running", "network_suspended_reason": None}],
        "skipped_count": 0, "error": None, "authoritative_total": 1,
    }
    inv.ingest_snapshot(conn_inv, "self", parse_result)
    conn_inv.close()

    class _Args:
        node = "self"
    rc = renewal_cli.cmd_check_exhausted(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "VPS 9" in out
    assert "QUEUED_FOR_REVIEW" in out


# ---------- run_polling_loop: scheduled report integration (no new timer, checked per tick) --

def test_polling_loop_sends_scheduled_report_when_due(tmp_path):
    import reports as reports_mod
    from datetime import datetime, timezone as _tz
    settings = tc.TelegramSettings(admin_ids=[111], reports_enabled=True, report_hour=0,
                                    report_minute=0, report_timezone="UTC")
    transport = _FakeTransport([{"ok": True, "result": []}])
    now = datetime(2026, 1, 1, 12, 0, tzinfo=_tz.utc)
    import unittest.mock as mock
    with mock.patch("reports.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.timezone = _tz
        bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                              max_iterations=1, sleep_fn=lambda s: None)
    send_calls = [c for c in transport.calls if c[0] == "sendMessage"]
    assert len(send_calls) >= 1
    assert send_calls[0][1]["chat_id"] == 111
    assert reports_mod.load_last_report_sent_date(_state_dir(tmp_path)) is not None


def test_polling_loop_does_not_send_report_when_disabled(tmp_path):
    import reports as reports_mod
    settings = tc.TelegramSettings(admin_ids=[111], reports_enabled=False)
    transport = _FakeTransport([{"ok": True, "result": []}])
    bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                          max_iterations=1, sleep_fn=lambda s: None)
    assert reports_mod.load_last_report_sent_date(_state_dir(tmp_path)) is None


def test_polling_loop_does_not_resend_report_same_day(tmp_path):
    import reports as reports_mod
    settings = tc.TelegramSettings(admin_ids=[111], reports_enabled=True, report_hour=0,
                                    report_minute=0, report_timezone="UTC")
    reports_mod.save_last_report_sent_date(
        _state_dir(tmp_path),
        reports_mod.datetime.now(reports_mod.timezone.utc).strftime("%Y-%m-%d"))
    transport = _FakeTransport([{"ok": True, "result": []}])
    bot.run_polling_loop(transport, settings, _config_dir(tmp_path), _state_dir(tmp_path),
                          max_iterations=1, sleep_fn=lambda s: None)
    send_calls = [c for c in transport.calls if c[0] == "sendMessage"]
    assert len(send_calls) == 0


# ---------- renewal_cli.py: send-report subcommand -------------------------------------------

def test_cmd_send_report_blocked_when_disabled(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)

    class _Args:
        period = "Manual"
    rc = renewal_cli.cmd_send_report(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_send_report_blocked_when_no_admins(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    tc.save_telegram_settings(renewal_cli.CONFIG_DIR,
                               tc.TelegramSettings(enabled=True, admin_ids=[]))

    class _Args:
        period = "Manual"
    rc = renewal_cli.cmd_send_report(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "no admin_ids" in out.lower()


def test_cmd_send_report_delivers_and_reports_per_admin(tmp_path, capsys):
    import renewal_cli
    import telegram_transport as tt

    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    tc.save_telegram_settings(renewal_cli.CONFIG_DIR,
                               tc.TelegramSettings(enabled=True, admin_ids=[111, 222]))
    tc.store_telegram_secrets(tc.default_secrets_path(renewal_cli.CONFIG_DIR), bot_token="t")

    def fake_call(self, method, params=None, http_post=None):
        return {"ok": True, "result": {}}

    import unittest.mock as mock
    with mock.patch.object(tt.DirectTransport, "call", fake_call):
        class _Args:
            period = "Manual"
        rc = renewal_cli.cmd_send_report(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "admin 111: delivered" in out
    assert "admin 222: delivered" in out
