"""Configuration for OPTIONAL scheduled backups (v1.8.0 Part 16) -- a systemd timer that
periodically runs `renewal_cli.py backup-full` unattended. DISABLED by default, same
required-defaults discipline as every other optional module in this project.

An unattended encrypted backup needs its GPG passphrase available without an interactive
prompt, so (when encryption is enabled) the passphrase is stored at rest -- with the same
0600-enforced discipline as every other secret in this project -- rather than invented out
of thin air or left as a silent plaintext fallback. This is an explicit, documented
tradeoff: anyone who can read this file as root could decrypt the archives it produces,
exactly as much access as they already have to the live databases themselves.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class BackupScheduleSecurityError(Exception):
    pass


@dataclass
class BackupScheduleSettings:
    enabled: bool = False
    interval_hours: int = 24
    retention_keep: int = 7
    encrypt: bool = False
    telegram_delivery_enabled: bool = False

    def __post_init__(self):
        if self.interval_hours < 1:
            raise ValueError("interval_hours must be at least 1")
        if self.retention_keep < 1:
            raise ValueError("retention_keep must be at least 1")
        if self.telegram_delivery_enabled and not self.encrypt:
            raise ValueError(
                "telegram_delivery_enabled requires encrypt=True -- refusing to configure "
                "unattended delivery of a plaintext backup")


def default_settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "backup", "settings.json")


def load_backup_schedule_settings(config_dir: str) -> BackupScheduleSettings:
    path = default_settings_path(config_dir)
    if not os.path.exists(path):
        return BackupScheduleSettings()
    try:
        data = json.loads(Path(path).read_text())
        return BackupScheduleSettings(
            enabled=bool(data.get("enabled", False)),
            interval_hours=int(data.get("interval_hours", 24)),
            retention_keep=int(data.get("retention_keep", 7)),
            encrypt=bool(data.get("encrypt", False)),
            telegram_delivery_enabled=bool(data.get("telegram_delivery_enabled", False)),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return BackupScheduleSettings()


def save_backup_schedule_settings(config_dir: str, settings: BackupScheduleSettings) -> None:
    path = default_settings_path(config_dir)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {"enabled": settings.enabled, "interval_hours": settings.interval_hours,
            "retention_keep": settings.retention_keep, "encrypt": settings.encrypt,
            "telegram_delivery_enabled": settings.telegram_delivery_enabled}
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def default_passphrase_path(config_dir: str) -> str:
    return os.path.join(config_dir, "backup", "passphrase.key")


def store_backup_passphrase(path: str, passphrase: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(passphrase)
    except Exception:
        os.unlink(tmp)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_backup_passphrase(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"no stored backup passphrase at {path} -- set one first")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise BackupScheduleSecurityError(
            f"{path} has insecure permissions ({oct(mode)}) -- expected 0600. Refusing to read.")
    return Path(path).read_text()
