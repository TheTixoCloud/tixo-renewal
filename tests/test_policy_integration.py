import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import policy as pol
import policy_integration as pi


def make_config(tmp_path, **global_overrides):
    base = {
        "initial_allocation_gb": "100", "monthly_grant_gb": "100",
        "carryover_enabled": True, "carryover_cap_gb": None,
        "billing_anchor": "verified_creation", "auto_renewal_enabled": True,
    }
    base.update(global_overrides)
    data = {"schema_version": 1, "global_defaults": base, "host_overrides": {}, "vps_overrides": {}}
    path = str(tmp_path / "policy.json")
    open(path, "w").write(json.dumps(data))
    return pol.load_policy_config(path)


def iso(dt):
    return dt.isoformat()


# 1. No retroactive grant for pre-existing VPS --------------------------------------------------

def test_vps_created_before_monitoring_started_is_pre_existing():
    created = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    first_poll = iso(datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert pi.is_pre_existing_vps(created, first_poll) is True


def test_vps_created_after_monitoring_started_is_genuinely_new():
    first_poll = iso(datetime(2026, 1, 1, tzinfo=timezone.utc))
    created = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    assert pi.is_pre_existing_vps(created, first_poll) is False


def test_no_first_poll_recorded_defaults_conservatively_to_pre_existing():
    created = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    assert pi.is_pre_existing_vps(created, None) is True


def test_preview_for_pre_existing_vps_explicitly_notes_no_retroactive_grant(tmp_path):
    cfg = make_config(tmp_path)
    created = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    first_poll = iso(datetime(2026, 9, 1, tzinfo=timezone.utc))
    result = pi.preview_renewal(cfg, "hostA", 1, created, first_poll,
                                 Decimal("1000"), Decimal("500"), iso(datetime(2027, 1, 1, tzinfo=timezone.utc)))
    assert "NO retroactive" in result["initial_grant_note"]


# 2. Excluded VPS short-circuits everything ------------------------------------------------------

def test_excluded_vps_preview_short_circuits(tmp_path):
    base = {"initial_allocation_gb": "100", "monthly_grant_gb": "100", "carryover_enabled": True,
            "carryover_cap_gb": None, "billing_anchor": "verified_creation", "auto_renewal_enabled": True}
    data = {"schema_version": 1, "global_defaults": base,
            "vps_overrides": {"hostA:5": {"excluded": True}}}
    path = str(tmp_path / "policy.json")
    open(path, "w").write(json.dumps(data))
    cfg = pol.load_policy_config(path)
    result = pi.preview_renewal(cfg, "hostA", 5, 1700000000, "2026-01-01T00:00:00+00:00",
                                 Decimal("1000"), Decimal("500"), "2026-06-01T00:00:00+00:00")
    assert result["status"] == "EXCLUDED"


# 3. Zero monthly grant flows through the preview correctly --------------------------------------

def test_zero_monthly_grant_host_preview(tmp_path):
    base = {"initial_allocation_gb": "100", "monthly_grant_gb": "100", "carryover_enabled": True,
            "carryover_cap_gb": None, "billing_anchor": "verified_creation", "auto_renewal_enabled": True}
    data = {"schema_version": 1, "global_defaults": base,
            "host_overrides": {"hostC": {"monthly_grant_gb": "0", "carryover_enabled": False}}}
    path = str(tmp_path / "policy.json")
    open(path, "w").write(json.dumps(data))
    cfg = pol.load_policy_config(path)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostC", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1000"), Decimal("200"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["monthly_grant_gb"] == "0"
    # UPDATED (2026-11-16, owner-approved minimum effective quota policy): carryover
    # disabled + 0 grant mathematically = 0 (still true, visible via
    # exact_calculated_quota_gb), but the EFFECTIVE quota is floored to 1 GB rather than
    # blocked, since Virtualizor cannot represent a bare 0 bandwidth limit (<=0 means
    # UNLIMITED there).
    assert result["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"
    assert result["exact_calculated_quota_gb"] == "0"
    assert result["proposed_new_quota_gb"] == "1"


# 4. No verified epoch and no explicit anchor -> fail closed -------------------------------------

def test_no_epoch_no_explicit_anchor_fails_closed(tmp_path):
    cfg = make_config(tmp_path)
    result = pi.preview_renewal(cfg, "hostA", 1, None, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1000"), Decimal("500"), iso(datetime(2026, 6, 1, tzinfo=timezone.utc)))
    assert result["status"] == "FROZEN_FOR_REVIEW"


# 5. Explicit billing anchor overrides verified creation ------------------------------------------

def test_explicit_anchor_used_even_when_verified_epoch_differs(tmp_path):
    explicit_epoch = int(datetime(2026, 5, 15, tzinfo=timezone.utc).timestamp())
    base = {"initial_allocation_gb": "100", "monthly_grant_gb": "100", "carryover_enabled": True,
            "carryover_cap_gb": None, "billing_anchor": "verified_creation", "auto_renewal_enabled": True}
    data = {"schema_version": 1, "global_defaults": base,
            "vps_overrides": {"hostA:9": {"billing_anchor": "explicit",
                                           "explicit_anchor_epoch": explicit_epoch}}}
    path = str(tmp_path / "policy.json")
    open(path, "w").write(json.dumps(data))
    cfg = pol.load_policy_config(path)
    wrong_verified_epoch = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 9, wrong_verified_epoch,
                                 iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1000"), Decimal("900"),
                                 iso(datetime(2026, 6, 15, tzinfo=timezone.utc)))  # aligned to explicit, not verified
    assert result["anchor_epoch"] == explicit_epoch
    assert result["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"


# 6. auto_renewal_enabled=False (the safe default) never reaches PLAUSIBLE_RENEWAL_CANDIDATE -----

def test_auto_renewal_disabled_caps_status_at_preview_only(tmp_path):
    cfg = make_config(tmp_path, auto_renewal_enabled=False)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1000"), Decimal("900"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "PREVIEW_ONLY_AUTO_RENEWAL_DISABLED"


# 7. Carryover cap flows through the preview --------------------------------------------------

def test_carryover_cap_in_preview(tmp_path):
    cfg = make_config(tmp_path, carryover_cap_gb="500")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("10000"), Decimal("1"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["proposed_new_quota_gb"] == "600"  # 500 cap + 100 grant
    assert result["carryover_cap_gb"] == "500"


# 8. PERMANENT BUSINESS RULE: unlimited-bandwidth VPS must NEVER be auto-renewed -----------------
# Owner-confirmed policy. Critical safety regression: previously apply_due()'s actual write
# path (via this exact function) had NO guard against quota<=0 at all -- only the separate
# dry-run/status reporting engine did. A quota-unchanged-but-usage-dropped pattern on an
# unlimited VPS, with auto_renewal_enabled=True, would have been computed as a normal
# PLAUSIBLE_RENEWAL_CANDIDATE and could have been auto-applied as a real quota WRITE,
# converting an unlimited VPS to a limited one.

def test_unlimited_quota_is_manual_only_even_with_auto_renewal_enabled_and_on_anniversary(tmp_path):
    cfg = make_config(tmp_path, auto_renewal_enabled=True)  # the dangerous configuration
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("0"), Decimal("900"),  # unlimited (quota<=0), usage dropped
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))  # exactly on anniversary
    assert result["status"] == "MANUAL_ONLY_UNLIMITED"
    assert "proposed_new_quota_gb" not in result  # formula must never even be computed


def test_negative_quota_treated_same_as_unlimited_manual_only(tmp_path):
    """Defensive: a corrupt/negative value must fail the same safe way, never be treated as
    some huge carryover."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("-5"), Decimal("1"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "MANUAL_ONLY_UNLIMITED"


def test_unlimited_check_runs_before_policy_excluded_check_and_before_eligibility(tmp_path):
    """The unlimited check must be unconditional -- it must trigger even for a VPS with no
    verified creation epoch at all (which would otherwise fail closed as FROZEN_FOR_REVIEW for
    a totally different reason) and regardless of any excluded flag."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    result = pi.preview_renewal(cfg, "hostA", 1, None, None,
                                 Decimal("0"), Decimal("0"), iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "MANUAL_ONLY_UNLIMITED"


def test_plan_change_limited_to_unlimited_switches_to_manual_only(tmp_path):
    """A VPS that WAS limited and becomes unlimited (plan change) must flip to
    MANUAL_ONLY_UNLIMITED on the very next evaluation -- no stale state, no special-casing of
    specific vpsids required, since the check is purely dynamic on the current quota value."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    before = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1000"), Decimal("900"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert before["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"
    after = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                Decimal("0"), Decimal("900"),  # plan changed to unlimited
                                iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert after["status"] == "MANUAL_ONLY_UNLIMITED"


def test_plan_change_unlimited_to_limited_restores_normal_eligibility(tmp_path):
    """The reverse transition: once a plan change makes the VPS limited again, it must be
    evaluated normally again -- no permanent/sticky exclusion left over from having been
    unlimited before."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    while_unlimited = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                          Decimal("0"), Decimal("0"),
                                          iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert while_unlimited["status"] == "MANUAL_ONLY_UNLIMITED"
    after_replan = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                       Decimal("1000"), Decimal("900"),
                                       iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert after_replan["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"


# 9. PERMANENT FIX: fractional-GB quota normalization at the preview layer -- reproduces the
# real VPS 106 incident (2026-11-15) and proves preview_renewal now reports the SAME value
# apply_due/apply would actually request, with the exact pre-rounding math preserved for audit.

def test_preview_rounds_fractional_proposed_quota_up_reproducing_vps106(tmp_path):
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "self", 106, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1100"), Decimal("190.41"),  # the real VPS 106 numbers
                                 iso(datetime(2026, 11, 15, tzinfo=timezone.utc)))
    assert result["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"
    assert result["exact_calculated_quota_gb"] == "1009.59"  # the real math, preserved for audit
    assert result["proposed_new_quota_gb"] == "1010"  # rounded UP -- NOT 1009 (the historical
    # truncated outcome) and NOT 1009.59 (unrepresentable in Virtualizor's integer column)


def test_preview_proposed_quota_unchanged_for_exact_integer_result(tmp_path):
    """No spurious +1 for the common whole-GB case."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("1000"), Decimal("900"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["proposed_new_quota_gb"] == "200"
    assert result["exact_calculated_quota_gb"] == "200"


# 10. CRITICAL: zero-quota write block -- Virtualizor's bandwidth<=0 means UNLIMITED, so a
# calculated target of exactly 0 GB (remaining=0 AND configured gift=0) must never be treated
# as a normal writable candidate, even with auto_renewal_enabled=True.

def test_zero_gift_and_zero_remaining_applies_minimum_1gb_floor_not_blocked(tmp_path):
    """OWNER-APPROVED POLICY (2026-11-16, supersedes the earlier BLOCKED_ZERO_QUOTA_UNSUPPORTED
    behavior): exact exhaustion with a 0 GB gift now yields an effective 1 GB quota, a normal
    PLAUSIBLE candidate -- example row 4 of the approved table."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True, monthly_grant_gb="0")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("5000"),  # exact exhaustion
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"
    assert result["proposed_new_quota_gb"] == "1"
    assert result["exact_calculated_quota_gb"] == "0"  # gift itself untouched, floor is separate


def test_zero_gift_with_slight_overrun_also_floors_to_1gb(tmp_path):
    """Example row 5 of the approved table: 5000/5001/gift=0 -> effective 1 GB."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True, monthly_grant_gb="0")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("5001"),  # 1 GB overrun
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"
    assert result["proposed_new_quota_gb"] == "1"


def test_example_row6_tiny_remaining_gift_zero_floors_at_exactly_1gb(tmp_path):
    """Example row 6: 5000/4999/gift=0 -> remaining=1, calculated=1, ceil=1, floor has no
    effect (already >=1) -> effective 1 GB."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True, monthly_grant_gb="0")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("4999"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["proposed_new_quota_gb"] == "1"
    assert result["exact_calculated_quota_gb"] == "1"


def test_example_row7_fractional_remaining_gift_zero_rounds_up_to_2gb(tmp_path):
    """Example row 7: 5000/4998.5/gift=0 -> remaining=1.5, calculated=1.5, ceil=2, floor has
    no effect (already >=1) -> effective 2 GB, NOT 1."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True, monthly_grant_gb="0")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("4998.5"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["proposed_new_quota_gb"] == "2"
    assert result["exact_calculated_quota_gb"] == "1.5"


def test_zero_gift_but_nonzero_remaining_is_not_blocked(tmp_path):
    """Only an actual zero RESULT blocks -- a 0 GB gift with real remaining balance is a
    perfectly normal, applicable renewal."""
    cfg = make_config(tmp_path, auto_renewal_enabled=True, monthly_grant_gb="0")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("4000"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "PLAUSIBLE_RENEWAL_CANDIDATE"
    assert result["proposed_new_quota_gb"] == "1000"  # 1000 remaining + 0 gift


def test_floor_applies_even_when_auto_renewal_disabled(tmp_path):
    """The floor is part of the calculation itself, not a status gate -- an operator must see
    the TRUE effective quota (1 GB) in preview-only mode too, not the unfloored 0."""
    cfg = make_config(tmp_path, auto_renewal_enabled=False, monthly_grant_gb="0")
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("5000"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["status"] == "PREVIEW_ONLY_AUTO_RENEWAL_DISABLED"
    assert result["proposed_new_quota_gb"] == "1"


def test_unlimited_vps_stays_manual_only_regardless_of_gift_value(tmp_path):
    """Item 9: gift settings (including 0) must never affect the unlimited-VPS exclusion."""
    for gift in ("0", "100", "9999"):
        cfg = make_config(tmp_path, auto_renewal_enabled=True, monthly_grant_gb=gift)
        result = pi.preview_renewal(cfg, "hostA", 1, None, None,
                                     Decimal("0"), Decimal("0"),
                                     iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
        assert result["status"] == "MANUAL_ONLY_UNLIMITED"


# 11. Overrun anomaly evidence flag -- additive, non-blocking, never changes the calculation.

def test_small_overrun_is_not_flagged_as_anomaly(tmp_path):
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("5001"),  # 1 GB over, trivial
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["overrun_gb"] == "1"
    assert result["overrun_anomaly_flag"] is False
    assert result["proposed_new_quota_gb"] == "100"  # calculation unaffected either way


def test_large_overrun_is_flagged_as_anomaly_but_calculation_unaffected(tmp_path):
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("10000"),  # 5000 GB over -- extreme
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["overrun_gb"] == "5000"
    assert result["overrun_anomaly_flag"] is True
    assert result["proposed_new_quota_gb"] == "100"  # still just clamped + gift, never blocked


def test_no_overrun_reports_zero_not_negative(tmp_path):
    cfg = make_config(tmp_path, auto_renewal_enabled=True)
    created = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    result = pi.preview_renewal(cfg, "hostA", 1, created, iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                                 Decimal("5000"), Decimal("1000"),
                                 iso(datetime(2026, 10, 15, tzinfo=timezone.utc)))
    assert result["overrun_gb"] == "0"
    assert result["overrun_anomaly_flag"] is False
