"""Tests for security.py (v1.8.0 Part 2 shared backend): password hashing, constant-time
comparison, numeric-only Telegram authorization, and SQLite-backed sliding-window rate
limiting.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import security as sec


# ---------- tokens --------------------------------------------------------------------------

def test_generate_secure_token_is_hex_and_right_length():
    token = sec.generate_secure_token(16)
    assert len(token) == 32  # hex-encoded, 2 chars per byte
    int(token, 16)  # raises if not valid hex


def test_generate_secure_token_never_repeats():
    tokens = {sec.generate_secure_token() for _ in range(100)}
    assert len(tokens) == 100


# ---------- constant-time compare -------------------------------------------------------------

def test_constant_time_equals_true_for_matching_strings():
    assert sec.constant_time_equals("secret123", "secret123") is True


def test_constant_time_equals_false_for_mismatched_strings():
    assert sec.constant_time_equals("secret123", "secret124") is False


def test_constant_time_equals_false_never_raises_on_non_string():
    assert sec.constant_time_equals(None, "x") is False
    assert sec.constant_time_equals(123, "123") is False
    assert sec.constant_time_equals("x", None) is False


# ---------- password hashing --------------------------------------------------------------------

def test_hash_password_roundtrips_with_verify():
    hash_hex, salt_hex = sec.hash_password("correct horse battery staple")
    assert sec.verify_password("correct horse battery staple", hash_hex, salt_hex) is True


def test_verify_password_rejects_wrong_password():
    hash_hex, salt_hex = sec.hash_password("correct horse battery staple")
    assert sec.verify_password("wrong password", hash_hex, salt_hex) is False


def test_hash_password_different_salts_produce_different_hashes():
    h1, s1 = sec.hash_password("same-password")
    h2, s2 = sec.hash_password("same-password")
    assert s1 != s2
    assert h1 != h2


def test_hash_password_same_salt_is_deterministic():
    _, salt_hex = sec.hash_password("x")
    salt_bytes = bytes.fromhex(salt_hex)
    h1, _ = sec.hash_password("same-password", salt_bytes)
    h2, _ = sec.hash_password("same-password", salt_bytes)
    assert h1 == h2


def test_verify_password_malformed_salt_fails_closed_never_raises():
    assert sec.verify_password("x", "deadbeef", "not-valid-hex!!") is False


def test_verify_password_never_raises_on_none_inputs():
    assert sec.verify_password(None, "deadbeef", "ab") is False


# ---------- Telegram numeric-ID-only authorization --------------------------------------------

def test_authorized_telegram_id_matches_allowlist():
    assert sec.is_authorized_telegram_id(123456789, [111, 123456789, 999]) is True


def test_unauthorized_telegram_id_not_in_allowlist():
    assert sec.is_authorized_telegram_id(42, [111, 123456789, 999]) is False


def test_telegram_id_as_string_still_matches_numeric_allowlist():
    assert sec.is_authorized_telegram_id("123456789", [123456789]) is True


def test_non_numeric_telegram_id_never_authorized():
    assert sec.is_authorized_telegram_id("not-a-number", [111]) is False


def test_malformed_admin_list_fails_closed():
    assert sec.is_authorized_telegram_id(111, [111, "not-a-number"]) is False


def test_telegram_authorization_is_never_username_based():
    """There is no username parameter at all in this function's signature -- this test
    documents and guards that design choice explicitly."""
    import inspect
    sig = inspect.signature(sec.is_authorized_telegram_id)
    assert "username" not in sig.parameters


# ---------- rate limiting ------------------------------------------------------------------

def test_rate_limit_allows_under_threshold(tmp_path):
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        result = sec.check_rate_limit(conn, "login:admin", max_events=5, window_seconds=60, now=now)
    assert result["allowed"] is True
    assert result["count"] == 3


def test_rate_limit_blocks_over_threshold(tmp_path):
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(5):
        sec.check_rate_limit(conn, "login:admin", max_events=5, window_seconds=60, now=now)
    result = sec.check_rate_limit(conn, "login:admin", max_events=5, window_seconds=60, now=now)
    assert result["allowed"] is False
    assert result["count"] == 6


def test_rate_limit_window_slides_old_events_drop_off(tmp_path):
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(5):
        sec.check_rate_limit(conn, "login:admin", max_events=5, window_seconds=60, now=base)
    later = base + timedelta(seconds=61)
    result = sec.check_rate_limit(conn, "login:admin", max_events=5, window_seconds=60, now=later)
    assert result["allowed"] is True  # the earlier 5 events are now outside the window
    assert result["count"] == 1


def test_rate_limit_buckets_are_independent(tmp_path):
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(5):
        sec.check_rate_limit(conn, "login:admin1", max_events=5, window_seconds=60, now=now)
    result = sec.check_rate_limit(conn, "login:admin2", max_events=5, window_seconds=60, now=now)
    assert result["allowed"] is True
    assert result["count"] == 1


def test_rate_limit_records_attempt_even_when_blocked(tmp_path):
    """A blocked attempt must still count toward the window -- otherwise an attacker gets a
    'free' retry every time they're blocked."""
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(10):
        sec.check_rate_limit(conn, "login:admin", max_events=5, window_seconds=60, now=now)
    count = conn.execute("SELECT COUNT(*) FROM rate_limit_events WHERE bucket=?",
                          ("login:admin",)).fetchone()[0]
    assert count == 10


def test_prune_rate_limit_events_removes_only_old_rows(tmp_path):
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 1, 2, tzinfo=timezone.utc)
    sec.check_rate_limit(conn, "b", max_events=100, window_seconds=1, now=old)
    sec.check_rate_limit(conn, "b", max_events=100, window_seconds=1, now=new)
    removed = sec.prune_rate_limit_events(conn, older_than_seconds=3600, now=new)
    assert removed == 1
    remaining = conn.execute("SELECT COUNT(*) FROM rate_limit_events").fetchone()[0]
    assert remaining == 1
