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
    assert result["proposed_new_quota_gb"] == "0"  # carryover disabled + 0 grant = 0, not 100


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
