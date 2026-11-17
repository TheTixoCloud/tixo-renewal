"""Web Control Center authentication: user store, session management, CSRF tokens, and
login rate limiting (v1.8.0 Part 11). Stdlib-only, built entirely on the already-implemented
`security.py` primitives (PBKDF2 password hashing, constant-time compare, SQLite sliding-
window rate limiting) -- no new cryptographic code written here, only orchestration.

STORAGE: users are stored in config_dir/web/users.json with 0600 permissions enforced,
exactly matching `credentials.py`'s established discipline (atomic write, fails closed on
loose permissions) -- a password hash is a credential-adjacent secret even though it's not
the plaintext password itself.

SESSIONS: server-side, SQLite-backed (config_dir's state dir), a random opaque token in an
HttpOnly cookie -- never a client-readable/forgeable JWT-style token carrying role claims.
Every session lookup re-reads the role from the CURRENT user record, so a role change or
account disable takes effect immediately, not only at next login.

ROLES: "admin" (full access, including any future mutating actions) and "viewer" (read-only)
-- mirrors the Telegram bot's existing admin/viewer distinction exactly, never a third,
divergent permission model.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
VALID_ROLES = (ROLE_ADMIN, ROLE_VIEWER)

SESSION_LIFETIME_SECONDS = 8 * 3600  # 8 hours
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300


class WebAuthSecurityError(Exception):
    pass


# ---------- user store -----------------------------------------------------------------------

def users_path(config_dir: str) -> str:
    return os.path.join(config_dir, "web", "users.json")


@dataclass
class WebUser:
    username: str
    password_hash: str
    password_salt: str
    role: str
    disabled: bool = False

    def __repr__(self):  # never leak a hash/salt into a log line via accidental repr
        return f"WebUser(username={self.username!r}, role={self.role!r}, disabled={self.disabled})"

    __str__ = __repr__


def _load_users_raw(config_dir: str) -> dict:
    path = users_path(config_dir)
    if not os.path.exists(path):
        return {}
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise WebAuthSecurityError(
            f"{path} has insecure permissions ({oct(mode)}) -- expected 0600. Refusing to "
            f"read it. Fix with `chmod 600 {path}` after confirming nobody untrusted "
            f"already had read access.")
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _save_users_raw(config_dir: str, data: dict) -> None:
    path = users_path(config_dir)
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        os.unlink(tmp)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def list_users(config_dir: str) -> list:
    raw = _load_users_raw(config_dir)
    return [WebUser(username=u, password_hash=v["password_hash"], password_salt=v["password_salt"],
                     role=v["role"], disabled=v.get("disabled", False))
            for u, v in raw.items()]


def get_user(config_dir: str, username: str) -> Optional[WebUser]:
    raw = _load_users_raw(config_dir)
    v = raw.get(username)
    if v is None:
        return None
    return WebUser(username=username, password_hash=v["password_hash"],
                    password_salt=v["password_salt"], role=v["role"], disabled=v.get("disabled", False))


def create_user(config_dir: str, username: str, password: str, role: str = ROLE_VIEWER) -> None:
    import security as sec
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r} (must be one of {VALID_ROLES})")
    if not username or not password:
        raise ValueError("username and password must both be non-empty")
    raw = _load_users_raw(config_dir)
    if username in raw:
        raise ValueError(f"user {username!r} already exists -- use set_password/set_role instead")
    hash_hex, salt_hex = sec.hash_password(password)
    raw[username] = {"password_hash": hash_hex, "password_salt": salt_hex, "role": role,
                      "disabled": False}
    _save_users_raw(config_dir, raw)


def set_password(config_dir: str, username: str, new_password: str) -> None:
    import security as sec
    raw = _load_users_raw(config_dir)
    if username not in raw:
        raise ValueError(f"no such user: {username!r}")
    hash_hex, salt_hex = sec.hash_password(new_password)
    raw[username]["password_hash"] = hash_hex
    raw[username]["password_salt"] = salt_hex
    _save_users_raw(config_dir, raw)


def set_user_disabled(config_dir: str, username: str, disabled: bool) -> None:
    raw = _load_users_raw(config_dir)
    if username not in raw:
        raise ValueError(f"no such user: {username!r}")
    raw[username]["disabled"] = disabled
    _save_users_raw(config_dir, raw)


def delete_user(config_dir: str, username: str) -> None:
    raw = _load_users_raw(config_dir)
    raw.pop(username, None)
    _save_users_raw(config_dir, raw)


def verify_login(config_dir: str, username: str, password: str) -> Optional[WebUser]:
    """Returns the WebUser on success, None on any failure (unknown user, wrong password,
    disabled account) -- deliberately the SAME generic failure shape for all three, so a
    login form never leaks which case occurred (no username enumeration)."""
    user = get_user(config_dir, username)
    if user is None or user.disabled:
        return None
    import security as sec
    if not sec.verify_password(password, user.password_hash, user.password_salt):
        return None
    return user


# ---------- sessions (SQLite-backed, server-side, opaque token) -------------------------------

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);
"""


def open_session_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SESSION_SCHEMA)
    return conn


def create_session(conn: sqlite3.Connection, username: str, now: datetime = None,
                    lifetime_seconds: float = SESSION_LIFETIME_SECONDS) -> str:
    now = now or datetime.now(timezone.utc)
    token = secrets.token_hex(32)
    expires_at = (now + timedelta(seconds=lifetime_seconds)).isoformat()
    conn.execute("INSERT INTO web_sessions (token, username, created_at, expires_at) VALUES (?,?,?,?)",
                 (token, username, now.isoformat(), expires_at))
    return token


def get_session_user(config_dir: str, conn: sqlite3.Connection, token: str,
                      now: datetime = None) -> Optional[WebUser]:
    """Returns the live WebUser for a valid, unexpired session token, or None. Re-reads the
    user record fresh every call (never caches role/disabled state in the session row
    itself) -- an account disabled or role-changed after login takes effect on the very
    next request, not only at the session's next renewal."""
    if not token:
        return None
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT username, expires_at FROM web_sessions WHERE token=?",
                        (token,)).fetchone()
    if row is None:
        return None
    username, expires_at = row
    try:
        if datetime.fromisoformat(expires_at) < now:
            conn.execute("DELETE FROM web_sessions WHERE token=?", (token,))
            return None
    except ValueError:
        return None
    user = get_user(config_dir, username)
    if user is None or user.disabled:
        return None
    return user


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM web_sessions WHERE token=?", (token,))


def prune_expired_sessions(conn: sqlite3.Connection, now: datetime = None) -> int:
    now = now or datetime.now(timezone.utc)
    cur = conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now.isoformat(),))
    return cur.rowcount


# ---------- CSRF tokens (derived from the session token + a server secret -- stateless,
# never stored per-session, so a GET page render can always compute the exact value a
# following POST must present without any extra DB lookup/table) -----------------------------

def csrf_secret_path(config_dir: str) -> str:
    return os.path.join(config_dir, "web", "csrf_secret.key")


def get_or_create_csrf_secret(config_dir: str) -> str:
    path = csrf_secret_path(config_dir)
    if os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise WebAuthSecurityError(
                f"{path} has insecure permissions ({oct(mode)}) -- expected 0600.")
        return Path(path).read_text().strip()
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = secrets.token_hex(32)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return value


def derive_csrf_token(csrf_secret: str, session_token: str) -> str:
    import hmac
    import hashlib
    return hmac.new(csrf_secret.encode("utf-8"), session_token.encode("utf-8"),
                     hashlib.sha256).hexdigest()


def generate_csrf_token() -> str:
    return secrets.token_hex(32)


def verify_csrf_token(session_csrf_token: str, submitted_token: str) -> bool:
    import security as sec
    return sec.constant_time_equals(session_csrf_token or "", submitted_token or "")


# ---------- login rate limiting (reuses security.py's shared limiter) -------------------------

def check_login_rate_limit(conn, remote_addr: str, now: datetime = None) -> dict:
    import security as sec
    return sec.check_rate_limit(conn, f"web-login:{remote_addr}",
                                 max_events=LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
                                 window_seconds=LOGIN_RATE_LIMIT_WINDOW_SECONDS, now=now)
