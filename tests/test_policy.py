import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import policy as pol


def write_config(tmp_path, data):
    path = str(tmp_path / "policy.json")
    open(path, "w").write(json.dumps(data))
    return path


def base_global(**overrides):
    d = {
        "initial_allocation_gb": "100", "monthly_grant_gb": "100",
        "carryover_enabled": True, "carryover_cap_gb": None,
        "billing_anchor": "verified_creation", "auto_renewal_enabled": False,
    }
    d.update(overrides)
    return d


# 1. Validation -----------------------------------------------------------------------------

def test_global_defaults_missing_field_rejected(tmp_path):
    data = {"schema_version": 1, "global_defaults": {"monthly_grant_gb": "100"}}
    path = write_config(tmp_path, data)
    with pytest.raises(pol.PolicyValidationError):
        pol.load_policy_config(path)


def test_wrong_schema_version_rejected(tmp_path):
    data = {"schema_version": 999, "global_defaults": base_global()}
    path = write_config(tmp_path, data)
    with pytest.raises(pol.PolicyValidationError):
        pol.load_policy_config(path)


def test_negative_amount_rejected(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(monthly_grant_gb="-5")}
    path = write_config(tmp_path, data)
    with pytest.raises(pol.PolicyValidationError):
        pol.load_policy_config(path)


def test_explicit_anchor_without_epoch_rejected(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(billing_anchor="explicit")}
    path = write_config(tmp_path, data)
    with pytest.raises(pol.PolicyValidationError):
        pol.load_policy_config(path)


def test_valid_config_loads(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global()}
    path = write_config(tmp_path, data)
    cfg = pol.load_policy_config(path)
    assert cfg.global_defaults.monthly_grant_gb == Decimal("100")


def test_write_default_policy_config_is_idempotent(tmp_path):
    path = str(tmp_path / "policy.json")
    pol.write_default_policy_config(path)
    data = json.loads(open(path).read())
    data["global_defaults"]["monthly_grant_gb"] = "250"
    open(path, "w").write(json.dumps(data))
    pol.write_default_policy_config(path)  # must not overwrite
    reloaded = json.loads(open(path).read())
    assert reloaded["global_defaults"]["monthly_grant_gb"] == "250"


def test_write_default_policy_config_honors_custom_monthly_base(tmp_path):
    """The monthly base/initial grant must be configurable AT install time (e.g. via
    TIXO_RENEWAL_MONTHLY_BASE_GB in deploy/install.sh), not just by hand-editing the file
    after the fact."""
    path = str(tmp_path / "policy.json")
    pol.write_default_policy_config(path, initial_allocation_gb=Decimal("250"),
                                     monthly_grant_gb=Decimal("250"))
    cfg = pol.load_policy_config(path)
    assert cfg.global_defaults.monthly_grant_gb == Decimal("250")
    assert cfg.global_defaults.initial_allocation_gb == Decimal("250")


def test_default_policy_auto_renewal_disabled_by_default(tmp_path):
    path = str(tmp_path / "policy.json")
    pol.write_default_policy_config(path)
    cfg = pol.load_policy_config(path)
    assert cfg.global_defaults.auto_renewal_enabled is False


# 2. Precedence: global -> host -> vps --------------------------------------------------------

def test_vps_with_no_overrides_gets_global_defaults(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(monthly_grant_gb="100")}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "hostA", 1)
    assert p.monthly_grant_gb == Decimal("100")
    assert p.source == "global"


def test_host_override_wins_over_global(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(monthly_grant_gb="100"),
            "host_overrides": {"hostB": {"monthly_grant_gb": "500"}}}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p_b = pol.resolve_policy(cfg, "hostB", 1)
    p_a = pol.resolve_policy(cfg, "hostA", 1)
    assert p_b.monthly_grant_gb == Decimal("500")
    assert p_a.monthly_grant_gb == Decimal("100")  # unaffected


def test_vps_override_wins_over_host_and_global(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(monthly_grant_gb="100"),
            "host_overrides": {"hostA": {"monthly_grant_gb": "500"}},
            "vps_overrides": {"hostA:42": {"monthly_grant_gb": "999", "carryover_cap_gb": "2000"}}}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "hostA", 42)
    assert p.monthly_grant_gb == Decimal("999")
    assert p.carryover_cap_gb == Decimal("2000")
    other_vps = pol.resolve_policy(cfg, "hostA", 43)
    assert other_vps.monthly_grant_gb == Decimal("500")  # falls back to host, not vps 42's value


# 3. Zero-value correctness (the explicit regression this module exists to prevent) ------------

def test_zero_monthly_grant_is_respected_not_defaulted_to_100(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(monthly_grant_gb="100"),
            "host_overrides": {"hostC": {"monthly_grant_gb": "0", "carryover_enabled": False}}}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "hostC", 1)
    assert p.monthly_grant_gb == Decimal("0")
    assert p.monthly_grant_gb is not None


def test_zero_initial_allocation_is_respected(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(initial_allocation_gb="0")}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "anyhost", 1)
    assert p.initial_allocation_gb == Decimal("0")


# 4. Carryover cap / disabled -------------------------------------------------------------------

def test_carryover_disabled_discards_remaining(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(carryover_enabled=False)}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 1)
    new_q = pol.compute_renewal_quota(p, Decimal("1000"), Decimal("200"))
    assert new_q == Decimal("100")  # just the monthly grant, remaining 800 discarded


def test_carryover_unlimited_by_default(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global()}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 1)
    new_q = pol.compute_renewal_quota(p, Decimal("100000"), Decimal("1"))
    assert new_q == Decimal("100099")  # 99999 remaining + 100 grant


def test_carryover_cap_clamps_large_remaining(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(carryover_cap_gb="500")}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 1)
    new_q = pol.compute_renewal_quota(p, Decimal("10000"), Decimal("1"))
    assert new_q == Decimal("600")  # 500 capped carryover + 100 grant


def test_carryover_cap_does_not_affect_small_remaining(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(carryover_cap_gb="500")}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 1)
    new_q = pol.compute_renewal_quota(p, Decimal("1000"), Decimal("900"))
    assert new_q == Decimal("200")  # 100 remaining (under cap) + 100 grant


def test_renewal_formula_matches_user_worked_example(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global()}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 1)
    assert pol.compute_renewal_quota(p, Decimal("8100"), Decimal("5000")) == Decimal("3200")


# 5. Excluded VPS ---------------------------------------------------------------------------

def test_excluded_vps_forces_auto_renewal_disabled(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(auto_renewal_enabled=True),
            "vps_overrides": {"h:99": {"excluded": True}}}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 99)
    assert p.excluded is True
    assert p.auto_renewal_enabled is False  # excluded always wins, even over an explicit True


# 6. Explicit billing anchor -----------------------------------------------------------------

def test_explicit_billing_anchor_at_vps_level(tmp_path):
    data = {"schema_version": 1, "global_defaults": base_global(),
            "vps_overrides": {"h:5": {"billing_anchor": "explicit", "explicit_anchor_epoch": 1700000000}}}
    cfg = pol.load_policy_config(write_config(tmp_path, data))
    p = pol.resolve_policy(cfg, "h", 5)
    assert p.billing_anchor == "explicit"
    assert p.explicit_anchor_epoch == 1700000000
