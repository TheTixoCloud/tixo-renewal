"""Generic security primitives shared by every TixoCloud Control Center surface that needs
authentication/authorization (Web login, Telegram admin verification, future remote-agent
enrollment) -- stdlib only, zero third-party dependency, matching this project's existing
zero-dependency philosophy (see renewal_cli.py's module docstring).

Nothing in this module talks to Virtualizor, the renewal ledger, or any network -- it is pure,
local, dependency-free cryptographic/authorization plumbing that Web/Telegram/CLI code can
all import identically, so password hashing, token generation, and rate limiting are never
reimplemented three different ways with three different (and possibly inconsistent) levels
of care.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# OWASP's 2023 minimum recommendation for PBKDF2-HMAC-SHA256. Deliberately NOT bcrypt/argon2
# (both would require a third-party dependency) -- PBKDF2 via hashlib is stdlib-only and,
# at this iteration count, adequate for an admin-only control panel with a small user count;
# documented here as a known tradeoff, not an oversight.
PBKDF2_ITERATIONS = 310_000


def generate_secure_token(n_bytes: int = 32) -> str:
    """Cryptographically secure random token (hex-encoded) -- for session tokens, CSRF
    tokens, remote-agent enrollment secrets, etc. Never derived from a predictable seed."""
    return secrets.token_hex(n_bytes)


def constant_time_equals(a, b) -> bool:
    """Timing-attack-resistant string comparison. Returns False (never raises) for any
    non-string input -- a type mismatch is a bug in the caller, not grounds to leak timing
    information via an exception path that behaves differently from the normal compare."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str, salt: bytes = None) -> tuple:
    """Returns (derived_hash_hex, salt_hex). A fresh random salt is generated if none is
    given (the normal case: setting a NEW password) -- callers verifying an EXISTING password
    must pass the stored salt back in, see verify_password()."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return derived.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    """Never raises on a malformed stored hash/salt (e.g. corrupted config) -- treats it as
    a failed verification, never as an authorization bypass."""
    try:
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError, AttributeError):
        return False
    try:
        derived_hex, _ = hash_password(password, salt)
    except (TypeError, AttributeError):
        return False
    return constant_time_equals(derived_hex, hash_hex)


def is_authorized_telegram_id(user_id, admin_ids: list) -> bool:
    """Authorization is ALWAYS by numeric Telegram user ID, NEVER by username -- per the
    explicit project instruction that usernames are spoofable/changeable and must never be
    trusted for access control. Any non-numeric value anywhere in `admin_ids` or `user_id`
    causes this to return False rather than raise or silently skip that one entry (a
    malformed admin-id list must fail closed, never partially-open)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        allowed = {int(a) for a in admin_ids}
    except (TypeError, ValueError):
        return False
    return uid in allowed


# ---------- rate limiting (shared by Web login throttling and Telegram command throttling) ---

RATE_LIMIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_time ON rate_limit_events(bucket, occurred_at);
"""


def open_rate_limit_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(RATE_LIMIT_SCHEMA)
    return conn


def check_rate_limit(conn: sqlite3.Connection, bucket: str, max_events: int,
                      window_seconds: float, now: datetime = None) -> dict:
    """Sliding-window rate limiter, SQLite-backed so it survives process restarts (a Web
    server or Telegram bot worker restarting must not reset an attacker's rate-limit
    window). `bucket` is caller-chosen (e.g. f"login:{username}" or
    f"telegram-cmd:{user_id}") -- this module imposes no bucket-naming convention beyond
    "callers must not reuse a bucket name for two logically different limits."

    Always records this attempt (even when it ends up blocked) BEFORE counting, so a client
    retrying immediately after being blocked doesn't get a "free" unblocked check by having
    its own attempt excluded from the count."""
    now = now or datetime.now(timezone.utc)
    window_start = (now - timedelta(seconds=window_seconds)).isoformat()
    conn.execute("INSERT INTO rate_limit_events (bucket, occurred_at) VALUES (?, ?)",
                 (bucket, now.isoformat()))
    count = conn.execute(
        "SELECT COUNT(*) FROM rate_limit_events WHERE bucket=? AND occurred_at >= ?",
        (bucket, window_start),
    ).fetchone()[0]
    return {"allowed": count <= max_events, "count": count, "max_events": max_events,
            "window_seconds": window_seconds}


def prune_rate_limit_events(conn: sqlite3.Connection, older_than_seconds: float,
                             now: datetime = None) -> int:
    """Housekeeping: deletes events older than the given age, bounding table growth for a
    long-running Web/Telegram process. Never called automatically by check_rate_limit() --
    an explicit, separate maintenance step, so callers control exactly when a DELETE runs."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=older_than_seconds)).isoformat()
    cur = conn.execute("DELETE FROM rate_limit_events WHERE occurred_at < ?", (cutoff,))
    return cur.rowcount
