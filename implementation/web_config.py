"""Web Control Center settings (v1.8.0 Part 11) -- separate, non-secret settings file,
mirroring telegram_config.py's TelegramSettings pattern exactly. `enabled=False` and
`bind_host="127.0.0.1"` are the required safe defaults -- the Web UI never listens on a
public interface unless an operator explicitly reconfigures it, and it does not even start
unless explicitly enabled (same pattern as the Telegram bot)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8443

VALID_LANGUAGES = ("en", "fa")
VALID_THEMES = ("dark", "light")


@dataclass
class WebSettings:
    enabled: bool = False
    bind_host: str = DEFAULT_BIND_HOST
    bind_port: int = DEFAULT_BIND_PORT
    domain: Optional[str] = None
    tls_cert_path: Optional[str] = None
    tls_key_path: Optional[str] = None
    default_language: str = "en"
    default_theme: str = "dark"

    def __post_init__(self):
        if not (1 <= self.bind_port <= 65535):
            raise ValueError("bind_port must be 1-65535")
        if self.default_language not in VALID_LANGUAGES:
            raise ValueError(f"invalid default_language: {self.default_language!r}")
        if self.default_theme not in VALID_THEMES:
            raise ValueError(f"invalid default_theme: {self.default_theme!r}")


def default_settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "web", "settings.json")


def load_web_settings(config_dir: str) -> WebSettings:
    path = default_settings_path(config_dir)
    if not os.path.exists(path):
        return WebSettings()
    try:
        data = json.loads(Path(path).read_text())
        return WebSettings(
            enabled=bool(data.get("enabled", False)),
            bind_host=data.get("bind_host", DEFAULT_BIND_HOST),
            bind_port=int(data.get("bind_port", DEFAULT_BIND_PORT)),
            domain=data.get("domain"),
            tls_cert_path=data.get("tls_cert_path"),
            tls_key_path=data.get("tls_key_path"),
            default_language=data.get("default_language", "en"),
            default_theme=data.get("default_theme", "dark"),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return WebSettings()


def save_web_settings(config_dir: str, settings: WebSettings) -> None:
    path = default_settings_path(config_dir)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {
        "enabled": settings.enabled, "bind_host": settings.bind_host,
        "bind_port": settings.bind_port, "domain": settings.domain,
        "tls_cert_path": settings.tls_cert_path, "tls_key_path": settings.tls_key_path,
        "default_language": settings.default_language, "default_theme": settings.default_theme,
    }
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
