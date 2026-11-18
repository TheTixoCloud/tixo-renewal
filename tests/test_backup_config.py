import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import backup_config as bcfg


def test_defaults_are_disabled_and_safe():
    settings = bcfg.BackupScheduleSettings()
    assert settings.enabled is False
    assert settings.encrypt is False
    assert settings.telegram_delivery_enabled is False
    assert settings.interval_hours == 24
    assert settings.retention_keep == 7


def test_rejects_invalid_interval():
    with pytest.raises(ValueError):
        bcfg.BackupScheduleSettings(interval_hours=0)


def test_rejects_invalid_retention():
    with pytest.raises(ValueError):
        bcfg.BackupScheduleSettings(retention_keep=0)


def test_telegram_delivery_requires_encryption():
    with pytest.raises(ValueError, match="requires encrypt=True"):
        bcfg.BackupScheduleSettings(encrypt=False, telegram_delivery_enabled=True)


def test_telegram_delivery_with_encryption_is_accepted():
    settings = bcfg.BackupScheduleSettings(encrypt=True, telegram_delivery_enabled=True)
    assert settings.telegram_delivery_enabled is True


def test_settings_roundtrip(tmp_path):
    settings = bcfg.BackupScheduleSettings(enabled=True, interval_hours=6, retention_keep=3,
                                             encrypt=True, telegram_delivery_enabled=True)
    bcfg.save_backup_schedule_settings(str(tmp_path), settings)
    loaded = bcfg.load_backup_schedule_settings(str(tmp_path))
    assert loaded.enabled is True
    assert loaded.interval_hours == 6
    assert loaded.retention_keep == 3
    assert loaded.encrypt is True
    assert loaded.telegram_delivery_enabled is True


def test_missing_settings_file_returns_safe_default(tmp_path):
    loaded = bcfg.load_backup_schedule_settings(str(tmp_path))
    assert loaded.enabled is False


def test_malformed_settings_file_falls_back_to_default(tmp_path):
    path = bcfg.default_settings_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    loaded = bcfg.load_backup_schedule_settings(str(tmp_path))
    assert loaded.enabled is False


def test_passphrase_roundtrip_and_permissions(tmp_path):
    path = str(tmp_path / "passphrase.key")
    bcfg.store_backup_passphrase(path, "correct-horse-battery-staple")
    loaded = bcfg.load_backup_passphrase(path)
    assert loaded == "correct-horse-battery-staple"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_passphrase_refuses_loose_permissions(tmp_path):
    path = str(tmp_path / "passphrase.key")
    bcfg.store_backup_passphrase(path, "secret")
    os.chmod(path, 0o644)
    with pytest.raises(bcfg.BackupScheduleSecurityError):
        bcfg.load_backup_passphrase(path)


def test_passphrase_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        bcfg.load_backup_passphrase("/nonexistent/passphrase.key")
