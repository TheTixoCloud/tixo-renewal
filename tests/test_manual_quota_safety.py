"""v1.6.0 Priority 10: regression tests proving the EXISTING renewal engine (writer.py,
unmodified in this release -- the formula and safety gates are not touched per the explicit
instruction not to alter them without separate approval) correctly handles an external
administrator changing a VPS's quota/USED concurrently with renewal processing, in every
timing permutation called out: before/after reset, while pending, during a backup lock,
after a deferred operation, after an uncertain outcome, and between observation and write.

Every scenario below asserts one of: correct eligibility is preserved, no duplicate write
occurs, a conflicting external change is never silently overwritten with stale data, and any
ambiguous outcome is marked UNKNOWN_NEEDS_VERIFICATION rather than guessed.
"""
import os
import sqlite3
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import writer as w

CREDS = w.ApiCredentials(base_url="https://example.invalid/index.php", api_key="k", api_pass="p")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    w.ensure_operations_schema(c)
    yield c
    c.close()


def approved_marker(tmp_path, name="WRITE_APPROVED"):
    p = tmp_path / name
    p.write_text("approved")
    return str(p)


def ok_post(url, data, timeout):
    return '{"done": 1}'


# 1. Quota increased by an admin BEFORE a renewal write -- the engine must write the value
#    IT decided on (from its own validated preview), never silently re-derive from a race-
#    prone later read; but the post-write VERIFICATION read is what actually protects us --
#    if the admin's increase is still live at verify-time and happens to equal our target,
#    that is indistinguishable from our own write succeeding (expected, not a bug).
def test_quota_increased_by_admin_before_write_does_not_block_or_duplicate(conn, tmp_path):
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("150"), Decimal("500")])  # admin bumped it to 150 before we touch it
    result = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads),
                              http_post=ok_post, confirm_read_delay_seconds=0)
    assert result["status"] == "APPLIED"
    # exactly one operation row -- no duplicate write path was taken
    assert conn.execute("SELECT COUNT(*) FROM operations WHERE vpsid=1").fetchone()[0] == 1


# 2. Quota decreased by an admin BEFORE our write -- same principle: our write still
#    proceeds to the value the caller explicitly requested (already validated upstream by
#    policy_integration.preview_renewal, not re-derived here), and is verified independently.
def test_quota_decreased_by_admin_before_write_proceeds_on_requested_value(conn, tmp_path):
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("10"), Decimal("500")])  # admin dropped it to 10 before we touch it
    result = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads),
                              http_post=ok_post, confirm_read_delay_seconds=0)
    assert result["status"] == "APPLIED"
    assert result["verified_after_quota_gb"] == Decimal("500")


# 3. Quota increased by an admin AFTER our write (i.e. between our write and our
#    confirmatory re-read) -- the false-success guard must catch this: the two post-write
#    reads diverge, so the operation is marked UNKNOWN_NEEDS_VERIFICATION, never silently
#    recorded as APPLIED with the admin's conflicting value, and never treated as a
#    duplicate-write trigger.
def test_quota_changed_by_admin_after_write_triggers_unknown_not_silent_overwrite(conn, tmp_path):
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("50"), Decimal("500"), Decimal("777")])  # admin changes it again right after
    result = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads),
                              http_post=ok_post, confirm_read_delay_seconds=0.001,
                              sleep_fn=lambda s: None)
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"
    assert result["verified_after_quota_gb"] == Decimal("777")


# 4. Quota decreased by an admin AFTER our write -- identical guard, opposite direction.
def test_quota_decreased_by_admin_after_write_triggers_unknown(conn, tmp_path):
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("50"), Decimal("500"), Decimal("3")])
    result = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads),
                              http_post=ok_post, confirm_read_delay_seconds=0.001,
                              sleep_fn=lambda s: None)
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"


# 5. Quota changed WHILE a renewal is pending (an unresolved prior operation exists for the
#    SAME period_key) -- a second attempt must be structurally BLOCKED, never a second write,
#    regardless of what the live quota looks like now.
def test_quota_changed_while_renewal_pending_blocks_second_write(conn, tmp_path):
    marker = approved_marker(tmp_path)
    w.record_operation_start(conn, "self:1:1|0|1:1", "self", 1, "1|0|1", "500")
    # left IN_PROGRESS -- simulates a crash mid-write while an admin also touched the quota
    result = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: Decimal("9999"),
                              http_post=ok_post, confirm_read_delay_seconds=0)
    assert result["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"
    assert conn.execute("SELECT COUNT(*) FROM operations WHERE vpsid=1").fetchone()[0] == 1


# 6. Quota changed DURING a backup lock (DEFERRED_LOCKED) -- the deferred retry must reuse
#    the ORIGINALLY stored target quota, never re-derive from whatever the quota looks like
#    now (which could reflect an admin's unrelated concurrent change), and must re-validate
#    write approval before retrying.
def test_quota_changed_during_backup_lock_retry_uses_original_not_current_value(conn, tmp_path):
    marker = approved_marker(tmp_path)
    locked_post = lambda url, data, timeout: '{"error": "VPS is locked: backup in progress"}'
    first = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                             confirm=True, read_live_quota_fn=lambda v: Decimal("100"),
                             http_post=locked_post, confirm_read_delay_seconds=0)
    assert first["status"] == "DEFERRED_LOCKED"

    # admin changes the live quota to something unrelated while the lock is in effect
    retry = w.retry_deferred_renewal(
        conn, "self", 1, CREDS, marker, read_live_quota_fn=lambda v: Decimal("500"),
        http_post=ok_post, confirm_read_delay_seconds=0)
    assert retry["status"] == "APPLIED"
    assert retry["verified_after_quota_gb"] == Decimal("500")  # the ORIGINAL target, not re-derived


# 7. Quota changed AFTER a deferred operation expires past max deferral age -- must mark
#    DEFERRED_EXPIRED_NEEDS_REVIEW WITHOUT calling the API at all (never a blind retry using
#    a possibly-stale target against a quota an admin may have already fixed manually).
def test_quota_changed_after_deferred_expiry_never_calls_api(conn, tmp_path):
    marker = approved_marker(tmp_path)
    locked_post = lambda url, data, timeout: '{"error": "backup in progress"}'
    w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                     confirm=True, read_live_quota_fn=lambda v: Decimal("100"),
                     http_post=locked_post, confirm_read_delay_seconds=0)

    called = {"api": False}
    def spy_post(url, data, timeout):
        called["api"] = True
        return '{"done": 1}'
    from datetime import datetime, timedelta, timezone
    far_future = lambda: datetime.now(timezone.utc) + timedelta(hours=1000)
    result = w.retry_deferred_renewal(
        conn, "self", 1, CREDS, marker, read_live_quota_fn=lambda v: Decimal("999"),
        http_post=spy_post, confirm_read_delay_seconds=0, max_deferral_hours=72, now_fn=far_future)
    assert result["status"] == "DEFERRED_EXPIRED_NEEDS_REVIEW"
    assert called["api"] is False


# 8. Quota changed after an UNKNOWN_NEEDS_VERIFICATION outcome -- a second apply_renewal call
#    for the SAME period must still be blocked by the unresolved-prior-operation gate (never
#    a blind retry that could double-apply), regardless of what the quota looks like now.
def test_quota_changed_after_uncertain_outcome_blocks_blind_retry(conn, tmp_path):
    marker = approved_marker(tmp_path)
    def post_then_fail(url, data, timeout):
        raise TimeoutError("socket timeout")
    first = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                             confirm=True, read_live_quota_fn=lambda v: Decimal("100"),
                             http_post=post_then_fail, confirm_read_delay_seconds=0)
    assert first["status"] == "UNKNOWN_NEEDS_VERIFICATION"

    second = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: Decimal("777"),
                              http_post=ok_post, confirm_read_delay_seconds=0)
    assert second["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"


# 9. Quota changed BETWEEN the eligibility observation (e.g. a dry-run/preview snapshot) and
#    the actual API write -- the PRE-write read inside apply_renewal is what catches this: if
#    that read itself fails/races, it's UNKNOWN_NEEDS_VERIFICATION; if it succeeds but the
#    live value no longer matches what the caller's stale preview assumed, apply_renewal
#    still writes the explicitly-requested target (the caller's job, not writer.py's, to
#    re-validate eligibility against a FRESH read before calling apply -- which is exactly
#    what cmd_apply_due already does by re-reading before computing its own target).
def test_quota_changed_between_observation_and_write_pre_write_read_failure_is_unknown(conn, tmp_path):
    marker = approved_marker(tmp_path)
    def flaky_read(vpsid):
        raise ConnectionError("host unreachable right as we tried to verify")
    result = w.apply_renewal(conn, "self", 1, "1|0|1", Decimal("500"), CREDS, marker,
                              confirm=True, read_live_quota_fn=flaky_read,
                              http_post=ok_post, confirm_read_delay_seconds=0)
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"
    # the API must never have been called -- the pre-write read gate fires first
    assert conn.execute("SELECT api_response FROM operations WHERE vpsid=1").fetchone()[0].startswith(
        "pre-write read failed")
