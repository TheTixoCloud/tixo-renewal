"""Telegram bot configuration: secrets (bot token, SOCKS5 proxy password, relay shared
secret) and non-secret settings (connectivity mode, admin/viewer IDs, report schedule).

Secrets use EXACTLY credentials.py's established storage discipline: atomic write, 0600
permissions enforced immediately, `load_*` fails closed (refuses to read) if the file's
permissions have been loosened by anything else -- never silently trusts a world/group-
readable secrets file. Kept in a separate file/module from Virtualizor API credentials
since they are a logically distinct secret set for a logically distinct, OPTIONAL feature.

The bot is DISABLED by default (`TelegramSettings.enabled = False`) and the connectivity
mode defaults to DIRECT -- both per the mission's explicit required defaults.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MODE_DIRECT = "direct"
MODE_SOCKS5 = "socks5"
MODE_RELAY = "relay"
VALID_CONNECTIVITY_MODES = (MODE_DIRECT, MODE_SOCKS5, MODE_RELAY)


class TelegramConfigSecurityError(Exception):
    pass


@dataclass
class TelegramSecrets:
    bot_token: str
    socks5_password: Optional[str] = None
    relay_shared_secret: Optional[str] = None

    def __repr__(self):  # never leak secrets via an accidental repr/print/log line
        return "TelegramSecrets(bot_token=***, socks5_password=***, relay_shared_secret=***)"

    __str__ = __repr__


def default_secrets_path(config_dir: str) -> str:
    return os.path.join(config_dir, "telegram", "secrets.json")


def store_telegram_secrets(path: str, bot_token: str, socks5_password: str = None,
                            relay_shared_secret: str = None) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"bot_token": bot_token, "socks5_password": socks5_password,
               "relay_shared_secret": relay_shared_secret}
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except Exception:
        os.unlink(tmp)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_telegram_secrets(path: str) -> TelegramSecrets:
    if not os.path.exists(path):
        raise FileNotFoundError(f"no stored Telegram secrets at {path} -- configure the bot first")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise TelegramConfigSecurityError(
            f"{path} has insecure permissions ({oct(mode)}) -- expected 0600. Refusing to "
            f"read it. Fix with `chmod 600 {path}` after confirming nobody untrusted "
            f"already had read access.")
    data = json.loads(Path(path).read_text())
    return TelegramSecrets(bot_token=data["bot_token"],
                            socks5_password=data.get("socks5_password"),
                            relay_shared_secret=data.get("relay_shared_secret"))


def delete_telegram_secrets(path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)


@dataclass
class TelegramSettings:
    """Non-secret settings -- plain JSON, no special permission requirement since nothing
    here is a credential. `enabled=False` and `connectivity_mode=MODE_DIRECT` are the
    required defaults; an absent/malformed settings file falls back to exactly these, never
    to a previously-configured-looking state."""
    enabled: bool = False
    connectivity_mode: str = MODE_DIRECT
    admin_ids: list = field(default_factory=list)
    viewer_ids: list = field(default_factory=list)
    socks5_host: Optional[str] = None
    socks5_port: Optional[int] = None
    socks5_username: Optional[str] = None
    relay_host: Optional[str] = None
    report_hour: int = 0
    report_minute: int = 0
    report_timezone: str = "Asia/Tehran"
    reports_enabled: bool = False

    def __post_init__(self):
        if self.connectivity_mode not in VALID_CONNECTIVITY_MODES:
            raise ValueError(f"invalid connectivity mode: {self.connectivity_mode!r} "
                              f"(must be one of {VALID_CONNECTIVITY_MODES})")
        if not (0 <= self.report_hour <= 23):
            raise ValueError("report_hour must be 0-23")
        if not (0 <= self.report_minute <= 59):
            raise ValueError("report_minute must be 0-59")


def default_settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "telegram", "settings.json")


def load_telegram_settings(config_dir: str) -> TelegramSettings:
    path = default_settings_path(config_dir)
    if not os.path.exists(path):
        return TelegramSettings()
    try:
        data = json.loads(Path(path).read_text())
        return TelegramSettings(
            enabled=bool(data.get("enabled", False)),
            connectivity_mode=data.get("connectivity_mode", MODE_DIRECT),
            admin_ids=list(data.get("admin_ids", [])),
            viewer_ids=list(data.get("viewer_ids", [])),
            socks5_host=data.get("socks5_host"),
            socks5_port=data.get("socks5_port"),
            socks5_username=data.get("socks5_username"),
            relay_host=data.get("relay_host"),
            report_hour=int(data.get("report_hour", 0)),
            report_minute=int(data.get("report_minute", 0)),
            report_timezone=data.get("report_timezone", "Asia/Tehran"),
            reports_enabled=bool(data.get("reports_enabled", False)),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return TelegramSettings()


def save_telegram_settings(config_dir: str, settings: TelegramSettings) -> None:
    path = default_settings_path(config_dir)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {
        "enabled": settings.enabled,
        "connectivity_mode": settings.connectivity_mode,
        "admin_ids": settings.admin_ids,
        "viewer_ids": settings.viewer_ids,
        "socks5_host": settings.socks5_host,
        "socks5_port": settings.socks5_port,
        "socks5_username": settings.socks5_username,
        "relay_host": settings.relay_host,
        "report_hour": settings.report_hour,
        "report_minute": settings.report_minute,
        "report_timezone": settings.report_timezone,
        "reports_enabled": settings.reports_enabled,
    }
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
