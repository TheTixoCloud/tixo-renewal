"""Tests for telegram_config.py (v1.8.0 Part 3): secure secrets storage (same discipline as
credentials.py) and settings persistence with the required safe defaults.
"""
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import telegram_config as tc


# ---------- secrets: 0600-enforced storage ----------------------------------------------------

def test_store_and_load_secrets_roundtrips(tmp_path):
    path = str(tmp_path / "secrets.json")
    tc.store_telegram_secrets(path, bot_token="123:ABC", socks5_password="p1",
                               relay_shared_secret="r1")
    loaded = tc.load_telegram_secrets(path)
    assert loaded.bot_token == "123:ABC"
    assert loaded.socks5_password == "p1"
    assert loaded.relay_shared_secret == "r1"


def test_stored_secrets_file_has_0600_permissions(tmp_path):
    path = str(tmp_path / "secrets.json")
    tc.store_telegram_secrets(path, bot_token="t")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_secrets_refuses_loose_permissions(tmp_path):
    path = str(tmp_path / "secrets.json")
    tc.store_telegram_secrets(path, bot_token="t")
    os.chmod(path, 0o644)
    with pytest.raises(tc.TelegramConfigSecurityError):
        tc.load_telegram_secrets(path)


def test_load_secrets_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        tc.load_telegram_secrets(str(tmp_path / "nope.json"))


def test_secrets_repr_never_leaks_token():
    secrets = tc.TelegramSecrets(bot_token="SUPERSECRET123", socks5_password="pw")
    assert "SUPERSECRET123" not in repr(secrets)
    assert "pw" not in repr(secrets)
    assert "SUPERSECRET123" not in str(secrets)


def test_delete_secrets_removes_file(tmp_path):
    path = str(tmp_path / "secrets.json")
    tc.store_telegram_secrets(path, bot_token="t")
    tc.delete_telegram_secrets(path)
    assert not os.path.exists(path)


def test_delete_secrets_nonexistent_file_is_noop(tmp_path):
    tc.delete_telegram_secrets(str(tmp_path / "nope.json"))  # must not raise


# ---------- settings: safe defaults ------------------------------------------------------------

def test_settings_default_disabled_and_direct_mode():
    settings = tc.TelegramSettings()
    assert settings.enabled is False
    assert settings.connectivity_mode == tc.MODE_DIRECT


def test_load_settings_absent_file_returns_safe_defaults(tmp_path):
    settings = tc.load_telegram_settings(str(tmp_path))
    assert settings.enabled is False
    assert settings.connectivity_mode == tc.MODE_DIRECT
    assert settings.admin_ids == []


def test_load_settings_malformed_file_falls_back_to_defaults(tmp_path):
    config_dir = str(tmp_path)
    path = tc.default_settings_path(config_dir)
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write("{not valid json")
    settings = tc.load_telegram_settings(config_dir)
    assert settings.enabled is False
    assert settings.connectivity_mode == tc.MODE_DIRECT


def test_save_then_load_settings_roundtrips(tmp_path):
    config_dir = str(tmp_path)
    settings = tc.TelegramSettings(enabled=True, connectivity_mode=tc.MODE_SOCKS5,
                                    admin_ids=[111, 222], viewer_ids=[333],
                                    socks5_host="proxy.example", socks5_port=1080,
                                    report_hour=3, report_minute=30)
    tc.save_telegram_settings(config_dir, settings)
    loaded = tc.load_telegram_settings(config_dir)
    assert loaded.enabled is True
    assert loaded.connectivity_mode == tc.MODE_SOCKS5
    assert loaded.admin_ids == [111, 222]
    assert loaded.viewer_ids == [333]
    assert loaded.socks5_host == "proxy.example"
    assert loaded.socks5_port == 1080
    assert loaded.report_hour == 3
    assert loaded.report_minute == 30


def test_invalid_connectivity_mode_rejected():
    with pytest.raises(ValueError):
        tc.TelegramSettings(connectivity_mode="carrier-pigeon")


def test_invalid_report_hour_rejected():
    with pytest.raises(ValueError):
        tc.TelegramSettings(report_hour=24)


def test_invalid_report_minute_rejected():
    with pytest.raises(ValueError):
        tc.TelegramSettings(report_minute=60)
