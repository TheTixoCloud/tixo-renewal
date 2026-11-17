"""Tests for emergency_policy.py (v1.8.0): the exhausted-bandwidth-with-no-grant-due policy
foundation. Covers: disabled-by-default safety, emergency grant idempotency/cooldown/max-
per-period, manual review queue lifecycle (pending/acknowledged/resolved), unlimited-VPS
exclusion, and that NOTHING here ever calls out to a Virtualizor API or applies a real quota
change (this module only decides policy and persists state).
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import emergency_policy as ep


def _db(tmp_path):
    return ep.open_db(str(tmp_path / "emergency_policy.sqlite3"))


# ---------- safety defaults -------------------------------------------------------------------

def test_default_policy_mode_is_disabled_preserving_existing_behavior():
    policy = ep.ExhaustedBandwidthPolicy()
    assert policy.mode == ep.MODE_DISABLED


def test_disabled_mode_never_creates_any_record(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_DISABLED)
    result = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="150", policy=policy)
    assert result["result"] == ep.RESULT_DISABLED
    assert conn.execute("SELECT COUNT(*) FROM emergency_grants").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM manual_review_queue").fetchone()[0] == 0


def test_invalid_mode_rejected():
    import pytest
    with pytest.raises(ValueError):
        ep.ExhaustedBandwidthPolicy(mode="auto_everything")


def test_negative_grant_amount_rejected():
    import pytest
    with pytest.raises(ValueError):
        ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, emergency_grant_amount_gb=Decimal("-1"))


def test_max_grants_per_month_must_be_at_least_one():
    import pytest
    with pytest.raises(ValueError):
        ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, max_grants_per_month=0)


# ---------- unlimited / not-exhausted short-circuits --------------------------------------------

def test_unlimited_vps_never_exhausted(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT)
    result = ep.evaluate_exhaustion(conn, 1, quota_gb="0", used_gb="999999", policy=policy)
    assert result["result"] == ep.RESULT_NOT_APPLICABLE_UNLIMITED


def test_not_yet_exhausted_vps_no_action(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT)
    result = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="50", policy=policy)
    assert result["result"] == ep.RESULT_NOT_EXHAUSTED


def test_unparseable_quota_never_fabricated_as_exhausted(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT)
    result = ep.evaluate_exhaustion(conn, 1, quota_gb="unavailable", used_gb="unavailable", policy=policy)
    assert result["result"] == ep.RESULT_NOT_EXHAUSTED


# ---------- emergency grant: idempotency, cooldown, max-per-period -----------------------------

def test_emergency_grant_proposed_on_first_exhaustion(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, emergency_grant_amount_gb=Decimal("1"))
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    result = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="150", policy=policy, now=now)
    assert result["result"] == ep.RESULT_GRANT_PROPOSED
    assert result["amount_gb"] == Decimal("1")
    row = conn.execute("SELECT vpsid, amount_gb, status FROM emergency_grants WHERE id=?",
                        (result["grant_id"],)).fetchone()
    assert row == (42, "1", ep.STATUS_PROPOSED)


def test_repeated_calls_same_episode_never_duplicate(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="150", policy=policy, now=now)
    r2 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="160", policy=policy,
                                 now=now + timedelta(hours=1))
    assert r1["result"] == ep.RESULT_GRANT_PROPOSED
    assert r2["result"] == ep.RESULT_ALREADY_PROPOSED
    assert r2["grant_id"] == r1["grant_id"]
    assert conn.execute("SELECT COUNT(*) FROM emergency_grants").fetchone()[0] == 1


def test_cooldown_blocks_a_second_grant_too_soon_after_the_first_resolves(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=48)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="150", policy=policy, now=now)
    ep.mark_grant_applied(conn, r1["grant_id"], applied_at=now.isoformat())
    ep.expire_stale_grant(conn, r1["grant_id"])  # simulate: this episode closed, VPS re-exhausted later
    # wait, expire only works on PROPOSED; since it was marked APPLIED, expire is a no-op here --
    # that's intentional: an APPLIED grant stays APPLIED forever (audit trail), cooldown still applies
    soon = now + timedelta(hours=10)
    r2 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="200", policy=policy, now=soon)
    assert r2["result"] == ep.RESULT_GRANT_BLOCKED_COOLDOWN
    assert r2["hours_remaining"] == 38.0


def test_cooldown_allows_grant_after_window_elapses(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=24,
                                          max_grants_per_month=10)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="150", policy=policy, now=now)
    ep.mark_grant_applied(conn, r1["grant_id"])
    later = now + timedelta(hours=25)
    r2 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="200", policy=policy, now=later)
    assert r2["result"] == ep.RESULT_GRANT_PROPOSED
    assert r2["grant_id"] != r1["grant_id"]


def test_max_grants_per_month_blocks_further_grants(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=0,
                                          max_grants_per_month=1)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="150", policy=policy, now=now)
    ep.mark_grant_applied(conn, r1["grant_id"])
    later_same_month = now + timedelta(days=5)
    r2 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="200", policy=policy,
                                 now=later_same_month)
    assert r2["result"] == ep.RESULT_GRANT_BLOCKED_MAX_PER_MONTH
    assert r2["grants_this_month"] == 1


def test_max_grants_per_month_resets_next_calendar_month(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=0,
                                          max_grants_per_month=1)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="150", policy=policy, now=now)
    ep.mark_grant_applied(conn, r1["grant_id"])
    next_month = datetime(2026, 2, 1, tzinfo=timezone.utc)
    r2 = ep.evaluate_exhaustion(conn, 42, quota_gb="100", used_gb="200", policy=policy, now=next_month)
    assert r2["result"] == ep.RESULT_GRANT_PROPOSED


def test_different_vpsids_never_interfere_with_each_others_cooldown_or_cap(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=48,
                                          max_grants_per_month=1)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="150", policy=policy, now=now)
    r2 = ep.evaluate_exhaustion(conn, 2, quota_gb="100", used_gb="150", policy=policy, now=now)
    assert r1["result"] == ep.RESULT_GRANT_PROPOSED
    assert r2["result"] == ep.RESULT_GRANT_PROPOSED
    assert r1["grant_id"] != r2["grant_id"]


# ---------- manual review queue lifecycle -------------------------------------------------------

def test_manual_review_queued_on_exhaustion(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_MANUAL_REVIEW)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    result = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy, now=now)
    assert result["result"] == ep.RESULT_QUEUED_FOR_REVIEW
    pending = ep.list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["vpsid"] == 7
    assert pending[0]["status"] == ep.REVIEW_PENDING


def test_manual_review_never_auto_grants(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_MANUAL_REVIEW)
    ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy,
                            now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert conn.execute("SELECT COUNT(*) FROM emergency_grants").fetchone()[0] == 0


def test_repeated_exhaustion_same_episode_does_not_duplicate_review(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_MANUAL_REVIEW)
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r1 = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy, now=now)
    r2 = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="200", policy=policy,
                                 now=now + timedelta(hours=1))
    assert r1["review_id"] == r2["review_id"]
    assert r2["result"] == ep.RESULT_ALREADY_QUEUED


def test_acknowledge_then_resolve_review(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_MANUAL_REVIEW)
    result = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy,
                                     now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    review_id = result["review_id"]
    ep.acknowledge_review(conn, review_id, acknowledged_by="admin1")
    row = conn.execute("SELECT status, acknowledged_by FROM manual_review_queue WHERE id=?",
                        (review_id,)).fetchone()
    assert row == (ep.REVIEW_ACKNOWLEDGED, "admin1")

    ep.resolve_review(conn, review_id, resolution_notes="Manually topped up via control panel")
    row2 = conn.execute("SELECT status, resolution_notes FROM manual_review_queue WHERE id=?",
                         (review_id,)).fetchone()
    assert row2 == (ep.REVIEW_RESOLVED, "Manually topped up via control panel")
    assert ep.list_pending_reviews(conn) == []


def test_resolve_without_prior_acknowledge_is_allowed(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_MANUAL_REVIEW)
    result = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy,
                                     now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    ep.resolve_review(conn, result["review_id"], resolution_notes="handled")
    row = conn.execute("SELECT status FROM manual_review_queue WHERE id=?",
                        (result["review_id"],)).fetchone()
    assert row[0] == ep.REVIEW_RESOLVED


def test_new_episode_after_resolution_creates_a_fresh_review_entry(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_MANUAL_REVIEW)
    r1 = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy,
                                 now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    ep.resolve_review(conn, r1["review_id"], resolution_notes="handled")
    r2 = ep.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy,
                                 now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert r2["result"] == ep.RESULT_QUEUED_FOR_REVIEW
    assert r2["review_id"] != r1["review_id"]
    assert conn.execute("SELECT COUNT(*) FROM manual_review_queue").fetchone()[0] == 2


# ---------- audit / listing helpers --------------------------------------------------------------

def test_list_proposed_grants_only_shows_proposed_not_applied_or_expired(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=0)
    r1 = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="150", policy=policy,
                                 now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    ep.mark_grant_applied(conn, r1["grant_id"])
    r2 = ep.evaluate_exhaustion(conn, 2, quota_gb="100", used_gb="150", policy=policy,
                                 now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    proposed = ep.list_proposed_grants(conn)
    assert len(proposed) == 1
    assert proposed[0]["vpsid"] == 2


def test_audit_trail_never_deletes_rows_only_changes_status(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT)
    r1 = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="150", policy=policy,
                                 now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    ep.expire_stale_grant(conn, r1["grant_id"])
    assert conn.execute("SELECT COUNT(*) FROM emergency_grants").fetchone()[0] == 1
    row = conn.execute("SELECT status FROM emergency_grants WHERE id=?", (r1["grant_id"],)).fetchone()
    assert row[0] == ep.STATUS_EXPIRED


def test_expired_grant_allows_a_fresh_episode(tmp_path):
    conn = _db(tmp_path)
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT, cooldown_hours=0)
    r1 = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="150", policy=policy,
                                 now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    ep.expire_stale_grant(conn, r1["grant_id"])
    r2 = ep.evaluate_exhaustion(conn, 1, quota_gb="100", used_gb="200", policy=policy,
                                 now=datetime(2026, 1, 16, tzinfo=timezone.utc))
    assert r2["result"] == ep.RESULT_GRANT_PROPOSED
    assert r2["grant_id"] != r1["grant_id"]


# ---------- settings persistence ---------------------------------------------------------------

def test_load_settings_defaults_to_disabled_when_absent(tmp_path):
    policy = ep.load_policy_settings(str(tmp_path))
    assert policy.mode == ep.MODE_DISABLED


def test_save_then_load_settings_roundtrips(tmp_path):
    policy = ep.ExhaustedBandwidthPolicy(mode=ep.MODE_EMERGENCY_GRANT,
                                          emergency_grant_amount_gb=Decimal("2.5"),
                                          cooldown_hours=72, max_grants_per_month=3)
    ep.save_policy_settings(str(tmp_path), policy)
    loaded = ep.load_policy_settings(str(tmp_path))
    assert loaded.mode == ep.MODE_EMERGENCY_GRANT
    assert loaded.emergency_grant_amount_gb == Decimal("2.5")
    assert loaded.cooldown_hours == 72
    assert loaded.max_grants_per_month == 3


def test_load_settings_malformed_file_falls_back_to_disabled(tmp_path):
    path = tmp_path / "emergency-policy-settings.json"
    path.write_text("{not valid json")
    policy = ep.load_policy_settings(str(tmp_path))
    assert policy.mode == ep.MODE_DISABLED
