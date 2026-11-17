"""Configuration for the lightweight hypervisor-side remote management agent (v1.8.0 Part
14). Runs ON the Virtualizor host itself (same host the renewal engine already runs on) --
this is NOT a separate install; it is an optional, disabled-by-default extra surface that
lets a central management server query this host's already-computed, already-tested
control_center_api.py snapshot remotely, over an authenticated channel, WITHOUT ever
handing the management server this host's Virtualizor API credentials.

Secrets (the shared secret used to authenticate the management server) follow EXACTLY the
same 0600-enforced discipline as credentials.py/telegram_config.py.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class AgentConfigSecurityError(Exception):
    pass


@dataclass
class AgentSecrets:
    shared_secret: str

    def __repr__(self):
        return "AgentSecrets(shared_secret=***)"

    __str__ = __repr__


def default_secrets_path(config_dir: str) -> str:
    return os.path.join(config_dir, "agent", "secrets.json")


def store_agent_secrets(path: str, shared_secret: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"shared_secret": shared_secret}, f)
    except Exception:
        os.unlink(tmp)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_agent_secrets(path: str) -> AgentSecrets:
    if not os.path.exists(path):
        raise FileNotFoundError(f"no stored agent secrets at {path} -- enroll it first")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise AgentConfigSecurityError(
            f"{path} has insecure permissions ({oct(mode)}) -- expected 0600. Refusing to read.")
    data = json.loads(Path(path).read_text())
    return AgentSecrets(shared_secret=data["shared_secret"])


def rotate_agent_secret(path: str, new_shared_secret: str) -> None:
    """Rotation is just a re-store -- the old secret is immediately and irrecoverably
    overwritten (atomic replace), so any management server still using the old secret stops
    being able to authenticate until it is updated with the new one too."""
    store_agent_secrets(path, new_shared_secret)


@dataclass
class AgentSettings:
    """DISABLED and localhost-only by default -- same required-defaults discipline as every
    other optional module in this project. `bind_host="0.0.0.0"` is needed for a REAL remote
    management server to reach this host, which an operator must explicitly choose (and put
    a firewall / TLS-terminating reverse proxy in front of -- this module speaks plain HTTP
    with HMAC-authenticated, integrity-checked envelopes, but does NOT itself provide
    transport confidentiality; see docs/DEVELOPMENT_PROGRESS.md)."""
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 8766
    max_envelope_age_seconds: float = 300

    def __post_init__(self):
        if not (1 <= self.bind_port <= 65535):
            raise ValueError("bind_port must be 1-65535")
        if self.max_envelope_age_seconds <= 0:
            raise ValueError("max_envelope_age_seconds must be positive")


def default_settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "agent", "settings.json")


def load_agent_settings(config_dir: str) -> AgentSettings:
    path = default_settings_path(config_dir)
    if not os.path.exists(path):
        return AgentSettings()
    try:
        data = json.loads(Path(path).read_text())
        return AgentSettings(
            enabled=bool(data.get("enabled", False)),
            bind_host=data.get("bind_host", "127.0.0.1"),
            bind_port=int(data.get("bind_port", 8766)),
            max_envelope_age_seconds=float(data.get("max_envelope_age_seconds", 300)),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return AgentSettings()


def save_agent_settings(config_dir: str, settings: AgentSettings) -> None:
    path = default_settings_path(config_dir)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {"enabled": settings.enabled, "bind_host": settings.bind_host,
            "bind_port": settings.bind_port,
            "max_envelope_age_seconds": settings.max_envelope_age_seconds}
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
