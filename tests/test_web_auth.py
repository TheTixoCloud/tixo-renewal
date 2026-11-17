"""Tests for web_auth.py (v1.8.0 Part 11): user store (0600-enforced, same discipline as
credentials.py), session lifecycle, CSRF token verification, and login rate limiting.
"""
import os
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import web_auth as wa


def _config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    return str(d)


# ---------- user store -------------------------------------------------------------------------

def test_create_and_verify_login_success(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "correct horse battery staple", role=wa.ROLE_ADMIN)
    user = wa.verify_login(config_dir, "admin", "correct horse battery staple")
    assert user is not None
    assert user.role == wa.ROLE_ADMIN


def test_verify_login_wrong_password_fails(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "correct password")
    assert wa.verify_login(config_dir, "admin", "wrong password") is None


def test_verify_login_unknown_user_fails_same_as_wrong_password(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "correct password")
    assert wa.verify_login(config_dir, "nobody", "whatever") is None


def test_verify_login_disabled_account_fails(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "correct password")
    wa.set_user_disabled(config_dir, "admin", True)
    assert wa.verify_login(config_dir, "admin", "correct password") is None


def test_create_user_rejects_duplicate(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw1")
    with pytest.raises(ValueError):
        wa.create_user(config_dir, "admin", "pw2")


def test_create_user_rejects_invalid_role(tmp_path):
    with pytest.raises(ValueError):
        wa.create_user(_config_dir(tmp_path), "admin", "pw", role="superuser")


def test_create_user_rejects_empty_username_or_password(tmp_path):
    config_dir = _config_dir(tmp_path)
    with pytest.raises(ValueError):
        wa.create_user(config_dir, "", "pw")
    with pytest.raises(ValueError):
        wa.create_user(config_dir, "admin", "")


def test_users_file_has_0600_permissions(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    mode = stat.S_IMODE(os.stat(wa.users_path(config_dir)).st_mode)
    assert mode == 0o600


def test_list_users_refuses_loose_permissions(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    os.chmod(wa.users_path(config_dir), 0o644)
    with pytest.raises(wa.WebAuthSecurityError):
        wa.list_users(config_dir)


def test_set_password_changes_login(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "old-password")
    wa.set_password(config_dir, "admin", "new-password")
    assert wa.verify_login(config_dir, "admin", "old-password") is None
    assert wa.verify_login(config_dir, "admin", "new-password") is not None


def test_set_password_unknown_user_raises(tmp_path):
    with pytest.raises(ValueError):
        wa.set_password(_config_dir(tmp_path), "nobody", "pw")


def test_delete_user_removes_login_ability(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    wa.delete_user(config_dir, "admin")
    assert wa.verify_login(config_dir, "admin", "pw") is None


def test_list_users_never_exposes_password_in_repr(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "supersecretpassword123")
    users = wa.list_users(config_dir)
    assert "supersecretpassword123" not in repr(users[0])
    assert "supersecretpassword123" not in str(users[0])


def test_list_users_empty_when_none_created(tmp_path):
    assert wa.list_users(_config_dir(tmp_path)) == []


# ---------- sessions ----------------------------------------------------------------------------

def _session_db(tmp_path):
    return wa.open_session_db(str(tmp_path / "web_sessions.sqlite3"))


def test_create_session_and_get_session_user(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw", role=wa.ROLE_ADMIN)
    conn = _session_db(tmp_path)
    token = wa.create_session(conn, "admin")
    user = wa.get_session_user(config_dir, conn, token)
    assert user is not None
    assert user.username == "admin"
    assert user.role == wa.ROLE_ADMIN


def test_get_session_user_unknown_token_returns_none(tmp_path):
    config_dir = _config_dir(tmp_path)
    conn = _session_db(tmp_path)
    assert wa.get_session_user(config_dir, conn, "nonexistent-token") is None


def test_get_session_user_empty_token_returns_none(tmp_path):
    config_dir = _config_dir(tmp_path)
    conn = _session_db(tmp_path)
    assert wa.get_session_user(config_dir, conn, "") is None
    assert wa.get_session_user(config_dir, conn, None) is None


def test_session_expires_after_lifetime(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    conn = _session_db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = wa.create_session(conn, "admin", now=now, lifetime_seconds=3600)
    still_valid = wa.get_session_user(config_dir, conn, token, now=now + timedelta(minutes=30))
    assert still_valid is not None
    expired = wa.get_session_user(config_dir, conn, token, now=now + timedelta(hours=2))
    assert expired is None


def test_expired_session_is_deleted_from_db(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    conn = _session_db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = wa.create_session(conn, "admin", now=now, lifetime_seconds=3600)
    wa.get_session_user(config_dir, conn, token, now=now + timedelta(hours=2))
    row = conn.execute("SELECT * FROM web_sessions WHERE token=?", (token,)).fetchone()
    assert row is None


def test_disabling_account_invalidates_existing_session_immediately(tmp_path):
    """A role change/disable must take effect on the VERY NEXT request, not only at the
    session's own expiry or next login."""
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    conn = _session_db(tmp_path)
    token = wa.create_session(conn, "admin")
    assert wa.get_session_user(config_dir, conn, token) is not None
    wa.set_user_disabled(config_dir, "admin", True)
    assert wa.get_session_user(config_dir, conn, token) is None


def test_destroy_session_logs_out(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    conn = _session_db(tmp_path)
    token = wa.create_session(conn, "admin")
    wa.destroy_session(conn, token)
    assert wa.get_session_user(config_dir, conn, token) is None


def test_prune_expired_sessions_removes_only_expired(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    conn = _session_db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_token = wa.create_session(conn, "admin", now=now, lifetime_seconds=10)
    new_token = wa.create_session(conn, "admin", now=now, lifetime_seconds=99999)
    removed = wa.prune_expired_sessions(conn, now=now + timedelta(seconds=20))
    assert removed == 1
    assert conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 1


def test_session_tokens_are_unique(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.create_user(config_dir, "admin", "pw")
    conn = _session_db(tmp_path)
    tokens = {wa.create_session(conn, "admin") for _ in range(50)}
    assert len(tokens) == 50


# ---------- CSRF ----------------------------------------------------------------------------

def test_csrf_token_matches_itself():
    token = wa.generate_csrf_token()
    assert wa.verify_csrf_token(token, token) is True


def test_csrf_token_mismatch_rejected():
    assert wa.verify_csrf_token(wa.generate_csrf_token(), wa.generate_csrf_token()) is False


def test_csrf_token_missing_submitted_rejected():
    assert wa.verify_csrf_token(wa.generate_csrf_token(), None) is False
    assert wa.verify_csrf_token(wa.generate_csrf_token(), "") is False


def test_csrf_tokens_are_unique():
    tokens = {wa.generate_csrf_token() for _ in range(50)}
    assert len(tokens) == 50


# ---------- login rate limiting ---------------------------------------------------------------

def test_login_rate_limit_blocks_after_threshold(tmp_path):
    import security as sec
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(wa.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
        result = wa.check_login_rate_limit(conn, "1.2.3.4", now=now)
    assert result["allowed"] is True
    blocked = wa.check_login_rate_limit(conn, "1.2.3.4", now=now)
    assert blocked["allowed"] is False


def test_login_rate_limit_is_per_remote_addr(tmp_path):
    import security as sec
    conn = sec.open_rate_limit_db(str(tmp_path / "ratelimit.sqlite3"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(wa.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
        wa.check_login_rate_limit(conn, "1.2.3.4", now=now)
    other = wa.check_login_rate_limit(conn, "5.6.7.8", now=now)
    assert other["allowed"] is True


# ---------- CSRF secret / derived token --------------------------------------------------------

def test_get_or_create_csrf_secret_persists(tmp_path):
    config_dir = _config_dir(tmp_path)
    s1 = wa.get_or_create_csrf_secret(config_dir)
    s2 = wa.get_or_create_csrf_secret(config_dir)
    assert s1 == s2


def test_csrf_secret_file_has_0600_permissions(tmp_path):
    config_dir = _config_dir(tmp_path)
    wa.get_or_create_csrf_secret(config_dir)
    import stat as _stat
    mode = _stat.S_IMODE(os.stat(wa.csrf_secret_path(config_dir)).st_mode)
    assert mode == 0o600


def test_derive_csrf_token_deterministic_for_same_session():
    secret = "s1"
    assert wa.derive_csrf_token(secret, "session-a") == wa.derive_csrf_token(secret, "session-a")


def test_derive_csrf_token_differs_per_session():
    secret = "s1"
    assert wa.derive_csrf_token(secret, "session-a") != wa.derive_csrf_token(secret, "session-b")


def test_derive_csrf_token_differs_per_secret():
    assert wa.derive_csrf_token("secret1", "session-a") != wa.derive_csrf_token("secret2", "session-a")
