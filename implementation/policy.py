"""Validated, versioned bandwidth policy configuration with three-level precedence:
global defaults -> per-host overrides -> per-VPS overrides (VPS wins).

CRITICAL: an override field is either EXPLICITLY SET (any value, including 0 or False) or
ABSENT (inherit from the parent level). This is implemented by checking `"key" in dict` during
parsing, never `dict.get(key, default)` -- the latter cannot distinguish "explicitly 0" from
"not configured," which is exactly the bug class this module exists to prevent (requirement:
"A monthly grant of 0 GB must never be interpreted as 100 GB").
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

# The only place a global fallback number is allowed to be hardcoded. Per-host/per-VPS
# overrides, once this module is in use, are what actually govern production -- this value
# only applies if the operator never configures anything at all (fresh install default).
BUILTIN_DEFAULT_INITIAL_GB = Decimal("100")
BUILTIN_DEFAULT_MONTHLY_GB = Decimal("100")


@dataclass(frozen=True)
class Policy:
    """A fully-resolved policy for one VPS -- every field has a concrete value, no `None`
    "inherit" markers remain after resolution."""
    initial_allocation_gb: Decimal
    monthly_grant_gb: Decimal
    carryover_enabled: bool
    carryover_cap_gb: Optional[Decimal]  # None = unlimited carryover
    billing_anchor: str  # "verified_creation" | "explicit"
    explicit_anchor_epoch: Optional[int]  # required if billing_anchor == "explicit"
    auto_renewal_enabled: bool
    excluded: bool
    source: str  # "global" | "host:<id>" | "vps:<id>" -- the MOST SPECIFIC level that set
    #                 the last field resolved; informational, for preview/audit, not logic.


@dataclass
class PolicyOverride:
    """A partial policy -- only the fields the operator explicitly configured are set
    (non-None). Used at all three levels (global/host/vps); the global level additionally
    requires every field to be set (validated separately)."""
    initial_allocation_gb: Optional[Decimal] = None
    monthly_grant_gb: Optional[Decimal] = None
    carryover_enabled: Optional[bool] = None
    carryover_cap_gb: Optional[Decimal] = None
    carryover_cap_gb_set: bool = False  # distinguishes "cap explicitly set to None/unlimited"
    #                                      from "cap not mentioned at all" at this level
    carryover_cap_unlimited: Optional[bool] = None
    billing_anchor: Optional[str] = None
    explicit_anchor_epoch: Optional[int] = None
    auto_renewal_enabled: Optional[bool] = None
    excluded: Optional[bool] = None

    @staticmethod
    def from_dict(d: dict) -> "PolicyOverride":
        kwargs = {}
        if "initial_allocation_gb" in d:
            kwargs["initial_allocation_gb"] = Decimal(str(d["initial_allocation_gb"]))
        if "monthly_grant_gb" in d:
            kwargs["monthly_grant_gb"] = Decimal(str(d["monthly_grant_gb"]))
        if "carryover_enabled" in d:
            kwargs["carryover_enabled"] = bool(d["carryover_enabled"])
        if "carryover_cap_gb" in d:
            kwargs["carryover_cap_gb_set"] = True
            if d["carryover_cap_gb"] is None:
                kwargs["carryover_cap_unlimited"] = True
            else:
                kwargs["carryover_cap_gb"] = Decimal(str(d["carryover_cap_gb"]))
                kwargs["carryover_cap_unlimited"] = False
        if "billing_anchor" in d:
            kwargs["billing_anchor"] = d["billing_anchor"]
        if "explicit_anchor_epoch" in d:
            kwargs["explicit_anchor_epoch"] = int(d["explicit_anchor_epoch"])
        if "auto_renewal_enabled" in d:
            kwargs["auto_renewal_enabled"] = bool(d["auto_renewal_enabled"])
        if "excluded" in d:
            kwargs["excluded"] = bool(d["excluded"])
        return PolicyOverride(**kwargs)


@dataclass
class PolicyConfig:
    schema_version: int
    global_defaults: PolicyOverride  # must be fully specified -- validated at load time
    host_overrides: dict  # host_id(str) -> PolicyOverride
    vps_overrides: dict   # "node_id:vpsid"(str) -> PolicyOverride


class PolicyValidationError(Exception):
    pass


REQUIRED_GLOBAL_FIELDS = (
    "initial_allocation_gb", "monthly_grant_gb", "carryover_enabled",
    "billing_anchor", "auto_renewal_enabled",
)


def validate_global_defaults(ov: PolicyOverride) -> None:
    for f in REQUIRED_GLOBAL_FIELDS:
        if getattr(ov, f) is None:
            raise PolicyValidationError(f"global_defaults missing required field: {f}")
    if ov.initial_allocation_gb < 0 or ov.monthly_grant_gb < 0:
        raise PolicyValidationError("allocation/grant amounts must be >= 0")
    if ov.carryover_cap_gb is not None and ov.carryover_cap_gb < 0:
        raise PolicyValidationError("carryover_cap_gb must be >= 0 when set")
    if ov.billing_anchor not in ("verified_creation", "explicit"):
        raise PolicyValidationError(f"invalid billing_anchor: {ov.billing_anchor!r}")
    if ov.billing_anchor == "explicit" and ov.explicit_anchor_epoch is None:
        raise PolicyValidationError("billing_anchor=explicit requires explicit_anchor_epoch")


def validate_override(ov: PolicyOverride, level: str) -> None:
    if ov.initial_allocation_gb is not None and ov.initial_allocation_gb < 0:
        raise PolicyValidationError(f"{level}: initial_allocation_gb must be >= 0")
    if ov.monthly_grant_gb is not None and ov.monthly_grant_gb < 0:
        raise PolicyValidationError(f"{level}: monthly_grant_gb must be >= 0")
    if ov.carryover_cap_gb is not None and ov.carryover_cap_gb < 0:
        raise PolicyValidationError(f"{level}: carryover_cap_gb must be >= 0 when set")
    if ov.billing_anchor is not None and ov.billing_anchor not in ("verified_creation", "explicit"):
        raise PolicyValidationError(f"{level}: invalid billing_anchor: {ov.billing_anchor!r}")
    if ov.billing_anchor == "explicit" and ov.explicit_anchor_epoch is None:
        raise PolicyValidationError(f"{level}: billing_anchor=explicit requires explicit_anchor_epoch")


def load_policy_config(path: str) -> PolicyConfig:
    data = json.loads(Path(path).read_text())
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PolicyValidationError(
            f"unsupported policy schema_version {version!r}, expected {SCHEMA_VERSION}")
    global_ov = PolicyOverride.from_dict(data["global_defaults"])
    validate_global_defaults(global_ov)
    host_overrides = {}
    for host_id, d in data.get("host_overrides", {}).items():
        ov = PolicyOverride.from_dict(d)
        validate_override(ov, f"host:{host_id}")
        host_overrides[host_id] = ov
    vps_overrides = {}
    for key, d in data.get("vps_overrides", {}).items():
        ov = PolicyOverride.from_dict(d)
        validate_override(ov, f"vps:{key}")
        vps_overrides[key] = ov
    return PolicyConfig(schema_version=version, global_defaults=global_ov,
                         host_overrides=host_overrides, vps_overrides=vps_overrides)


def write_default_policy_config(path: str, initial_allocation_gb: Decimal = None,
                                 monthly_grant_gb: Decimal = None) -> None:
    """Idempotent: never overwrites an existing, possibly-operator-edited file.

    initial_allocation_gb/monthly_grant_gb let the installer set the monthly base AT install
    time (e.g. from an env var) instead of requiring a manual post-install edit -- still just
    writes the same editable policy.json, never a special case the rest of this module has to
    know about."""
    if Path(path).exists():
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    default = {
        "schema_version": SCHEMA_VERSION,
        "global_defaults": {
            "initial_allocation_gb": str(initial_allocation_gb if initial_allocation_gb is not None else BUILTIN_DEFAULT_INITIAL_GB),
            "monthly_grant_gb": str(monthly_grant_gb if monthly_grant_gb is not None else BUILTIN_DEFAULT_MONTHLY_GB),
            "carryover_enabled": True,
            "carryover_cap_gb": None,
            "billing_anchor": "verified_creation",
            "auto_renewal_enabled": False,  # SAFE DEFAULT: nothing is auto-renewed until an
            # operator deliberately turns this on globally, per-host, or per-VPS.
        },
        "host_overrides": {},
        "vps_overrides": {},
    }
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(default, indent=2) + "\n")
    import os
    os.replace(tmp, path)


def _merge_field(result_ov: PolicyOverride, parent_level_name: str, child: PolicyOverride,
                  child_level_name: str, field_name: str, source_map: dict):
    val = getattr(child, field_name)
    if val is not None:
        setattr(result_ov, field_name, val)
        source_map[field_name] = child_level_name
    if field_name == "carryover_cap_gb" and child.carryover_cap_gb_set:
        result_ov.carryover_cap_gb_set = True
        result_ov.carryover_cap_unlimited = child.carryover_cap_unlimited
        result_ov.carryover_cap_gb = child.carryover_cap_gb
        source_map["carryover_cap_gb"] = child_level_name


ALL_FIELDS = ("initial_allocation_gb", "monthly_grant_gb", "carryover_enabled",
              "carryover_cap_gb", "billing_anchor", "explicit_anchor_epoch",
              "auto_renewal_enabled", "excluded")


def resolve_policy(config: PolicyConfig, node_id: str, vpsid: int) -> Policy:
    """Resolve global -> host -> vps precedence. Returns a fully-concrete Policy. Never
    raises for a VPS/host with no overrides -- falls through cleanly to global_defaults."""
    merged = PolicyOverride(**vars(config.global_defaults))
    source = {f: "global" for f in ALL_FIELDS}
    source["carryover_cap_gb"] = "global"

    host_ov = config.host_overrides.get(node_id)
    if host_ov:
        for f in ALL_FIELDS:
            _merge_field(merged, "global", host_ov, f"host:{node_id}", f, source)

    vps_key = f"{node_id}:{vpsid}"
    vps_ov = config.vps_overrides.get(vps_key)
    if vps_ov:
        for f in ALL_FIELDS:
            _merge_field(merged, "host", vps_ov, f"vps:{vps_key}", f, source)

    excluded = bool(merged.excluded)
    cap = None
    if merged.carryover_cap_gb_set:
        cap = None if merged.carryover_cap_unlimited else merged.carryover_cap_gb

    return Policy(
        initial_allocation_gb=merged.initial_allocation_gb,
        monthly_grant_gb=merged.monthly_grant_gb,
        carryover_enabled=bool(merged.carryover_enabled),
        carryover_cap_gb=cap,
        billing_anchor=merged.billing_anchor,
        explicit_anchor_epoch=merged.explicit_anchor_epoch,
        auto_renewal_enabled=False if excluded else bool(merged.auto_renewal_enabled),
        excluded=excluded,
        source=source.get("monthly_grant_gb", "global"),
    )


def compute_renewal_quota(policy: Policy, old_quota: Decimal, old_used: Decimal) -> Decimal:
    """new_quota = allowed_unused_carryover + configured_monthly_grant. Respects
    carryover_enabled=False (discard remaining) and carryover_cap_gb (clamp remaining)."""
    remaining = old_quota - old_used
    if remaining < 0:
        remaining = Decimal(0)
    if not policy.carryover_enabled:
        carryover = Decimal(0)
    elif policy.carryover_cap_gb is not None and remaining > policy.carryover_cap_gb:
        carryover = policy.carryover_cap_gb
    else:
        carryover = remaining
    return carryover + policy.monthly_grant_gb


def round_quota_for_virtualizor(quota_gb: Optional[Decimal]) -> Optional[Decimal]:
    """Virtualizor's own `vps.bandwidth` column is a plain SQL integer -- confirmed via
    `DESCRIBE vps` on ir1 (`bandwidth int(10)`, while `used_bandwidth` is `decimal(12,2)`):
    it cannot store a fractional GB value at all. A real production apply (vpsid 106,
    2026-11-15) requested 1009.59 GB; Virtualizor silently TRUNCATED it to 1009 GB. The write
    genuinely succeeded, but our own live read-back correctly refused to call it APPLIED
    because 1009 != 1009.59 -- a real, reachable mismatch, not hypothetical, and it required a
    manual `resolve-operation` to close out afterward.

    Business decision (owner-confirmed, 2026-11-16): round UP (ceiling) to the nearest whole
    GB, never down -- a customer must never receive less than their calculated carryover
    entitlement because of a rounding artifact. The at-most-0.99GB-per-cycle upside is
    negligible against a >=100GB monthly base and is strictly customer-favorable. Compatible
    with the existing carryover_cap_gb logic (rounding happens AFTER the cap is applied, so a
    cap is never exceeded by more than the same <1GB rounding amount) and with
    carryover_enabled=False (rounding still applies to the monthly_grant_gb-only result).

    This has only been verified against ir1's schema. Another node's `vps.bandwidth` column
    type should be independently confirmed before assuming the same integer-only behavior --
    this function is still safe to apply unconditionally even if a future node DOES support
    fractional GB, since rounding up to the nearest whole GB is simply a more conservative
    (never-less-than-entitled) value in that case too."""
    if quota_gb is None:
        return None
    return quota_gb.to_integral_value(rounding=ROUND_CEILING)


def apply_minimum_renewal_floor(rounded_quota_gb: Optional[Decimal]) -> Optional[Decimal]:
    """Owner-approved minimum EFFECTIVE quota policy (2026-11-16): a genuinely eligible,
    validated LIMITED-bandwidth renewal must never compute to less than 1 GB, regardless of
    the configured gift (including an explicitly configured 0 GB gift) -- Virtualizor's
    bandwidth<=0 means UNLIMITED, so anything below 1 is not representable as "a small
    allowance," only as "no limit at all." The floor is a MINIMUM EFFECTIVE QUOTA, not an
    additional gift: the configured gift value itself is never altered by this function, only
    the final result of (remaining + gift) after rounding.

    CRITICAL SCOPE: this floor must be applied ONLY inside the validated renewal-policy
    calculation (policy_integration.preview_renewal, and shadow_monitor's mirrored reporting
    recompute) -- i.e. only to a value that was actually computed FROM a verified pre-reset
    snapshot pair + resolved policy. It must NEVER be applied to an arbitrary raw manual
    --new-quota-gb value typed directly at the `apply` CLI, which bypasses preview_renewal
    entirely -- that path must keep blocking 0/negative outright (writer.apply_renewal's own
    guard, deliberately NOT touched by this function) rather than silently floor it to 1,
    since there is no reliable way to distinguish "this 1 came from a validated calculation"
    from "an operator happened to type 1, or 0, by hand." Call this function ONLY at the one
    shared choke point both apply_due and cmd_preview go through -- never from writer.py."""
    if rounded_quota_gb is None:
        return None
    return max(Decimal(1), rounded_quota_gb)
