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


def test_reports_command_is_honest_about_not_implemented(tmp_path):
    settings = tc.TelegramSettings(admin_ids=[111])
    result = bot.process_update(_message_update("/reports", user_id=111), settings,
                                 _config_dir(tmp_path), _state_dir(tmp_path))
    assert "not yet implemented" in result["reply"].lower()


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
