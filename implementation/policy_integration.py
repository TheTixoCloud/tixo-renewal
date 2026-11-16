"""Ties policy.py (what a VPS's bandwidth rules are) together with the shadow-monitor SQLite
state (what we've actually observed) and engine.check_monthly_anniversary (eligibility math)
to produce a per-VPS renewal PREVIEW -- never an applied change. This is the one place that
enforces "existing VPS must not receive a retroactive initial grant merely because the
software was installed."
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import policy as pol
from engine import check_monthly_anniversary


def billing_anchor_epoch(p: pol.Policy, verified_created_epoch: Optional[int]) -> Optional[int]:
    """Resolves which epoch to actually use for anniversary math, per the policy's configured
    billing_anchor. 'explicit' always wins when configured -- it exists specifically for VPS
    whose real creation date is unknown/wrong but an admin wants a defined billing cycle day."""
    if p.billing_anchor == "explicit":
        return p.explicit_anchor_epoch
    return verified_created_epoch  # "verified_creation" -- None if we have no verified epoch


def is_pre_existing_vps(vps_created_epoch: int, node_first_poll_at: Optional[str]) -> bool:
    """True if this VPS already existed before our software ever started watching this node
    -- i.e. it must NEVER receive a retroactive initial_allocation_gb grant just because we
    started monitoring it. If we have no first_poll_at yet (should not normally happen,
    defensive), treat conservatively as pre-existing (no grant) rather than risk a false
    'new VPS' classification."""
    if node_first_poll_at is None:
        return True
    try:
        first_poll_dt = datetime.fromisoformat(node_first_poll_at)
    except ValueError:
        return True
    created_dt = datetime.fromtimestamp(vps_created_epoch, tz=timezone.utc)
    return created_dt <= first_poll_dt


def preview_renewal(policy_config: pol.PolicyConfig, node_id: str, vpsid: int,
                     verified_created_epoch: Optional[int], node_first_poll_at: Optional[str],
                     old_quota: Decimal, old_used: Decimal, event_ts: str) -> dict:
    """Pure computation, no I/O, no side effects. Returns a dict describing exactly what WOULD
    happen, with full provenance, never applying anything.

    PERMANENT BUSINESS RULE (owner-confirmed): a VPS with unlimited bandwidth must NEVER be
    auto-renewed -- checked FIRST, unconditionally, before policy resolution/exclusion/
    eligibility, so it can never be bypassed by any policy.json state (including
    auto_renewal_enabled=true at any scope). Virtualizor's own convention is `bandwidth<=0`
    means unlimited (confirmed empirically against real production data -- every known
    unlimited VPS on ir1 has bandwidth=0; this is also what engine.py's
    reset-detection already treats as "unlimited or invalid quota", kept consistent here).
    This is the one real gap this check closes: engine.py's CarryoverEngine (used by
    dry-run/status reporting) already refused to propose anything for quota<=0, but
    apply_due()'s actual write path went through _renewal_baseline_for_vpsid() + this
    function directly and had NO such guard -- a quota-unchanged-but-usage-dropped pattern on
    an unlimited VPS (e.g. Virtualizor's own usage-counter accounting resetting it) could
    previously have been auto-applied as a real quota WRITE, converting an unlimited VPS to a
    limited one. That could never actually be exercised live (auto_renewal_enabled is still
    globally false), but it was a real, reachable code path, not a hypothetical."""
    if old_quota is not None and old_quota <= 0:
        return {"vpsid": vpsid, "status": "MANUAL_ONLY_UNLIMITED", "policy_source": None,
                "detail": f"bandwidth={old_quota} GB (<=0, Virtualizor's unlimited "
                          f"representation) -- permanently excluded from automatic renewal; "
                          f"must be handled manually regardless of policy or "
                          f"auto_renewal_enabled"}

    # Evidence-preservation only -- does NOT change the calculation. Slight USED>quota overrun
    # is expected Virtualizor accounting slop (confirmed business rule: never treated as
    # corruption or a reason to subtract from the gift; compute_renewal_quota already clamps
    # remaining to zero regardless of overrun size). An ANOMALOUSLY LARGE overrun is still
    # clamped the same way (never blocks or alters the formula) but is flagged here for a
    # human to review, since it could indicate a genuine data problem worth investigating
    # separately from the renewal itself. Threshold is a deliberately conservative, documented
    # DEFAULT judgment call (not a proven business rule) -- mirrors the same style already
    # used by engine.py's reset-vs-not-reset heuristic (absolute-or-relative combination).
    overrun_gb = (old_used - old_quota) if (old_used is not None and old_used > old_quota) else Decimal(0)
    OVERRUN_ANOMALY_ABS_GB = Decimal("50")
    OVERRUN_ANOMALY_REL = Decimal("0.05")  # 5% of quota
    overrun_anomaly_flag = overrun_gb > 0 and (
        overrun_gb > OVERRUN_ANOMALY_ABS_GB or overrun_gb > old_quota * OVERRUN_ANOMALY_REL)

    p = pol.resolve_policy(policy_config, node_id, vpsid)

    if p.excluded:
        return {"vpsid": vpsid, "status": "EXCLUDED", "policy_source": p.source,
                "detail": "this VPS is explicitly excluded from automatic renewal by policy"}

    pre_existing = is_pre_existing_vps(verified_created_epoch, node_first_poll_at) \
        if verified_created_epoch else True

    if pre_existing:
        initial_grant_note = ("pre-existing VPS (created before this software started "
                               "watching this node) -- NO retroactive initial allocation is "
                               "proposed, per explicit requirement")
    else:
        initial_grant_note = (f"genuinely new VPS (created after monitoring began) -- a "
                               f"{p.initial_allocation_gb} GB initial allocation would apply "
                               f"at creation, separately from any renewal")

    anchor_epoch = billing_anchor_epoch(p, verified_created_epoch)
    if anchor_epoch is None:
        return {"vpsid": vpsid, "status": "FROZEN_FOR_REVIEW", "policy_source": p.source,
                "detail": "no verified creation epoch and no explicit billing_anchor "
                          "configured -- cannot evaluate eligibility, fail closed",
                "initial_grant_note": initial_grant_note}

    eligible, detail = check_monthly_anniversary(anchor_epoch, event_ts)
    exact_proposed_quota = pol.compute_renewal_quota(p, old_quota, old_used)
    # Rounded HERE, once, at the earliest point the final quota is computed -- every
    # downstream consumer (apply_due, cmd_preview, cmd_apply via writer.apply_renewal's own
    # defensive rounding, post-write verification, ledger recording) ends up using this exact
    # same normalized value. See round_quota_for_virtualizor()'s docstring for why (VPS 106,
    # 2026-11-15 incident). The pre-rounding exact calculation is preserved below for audit.
    proposed_quota = pol.round_quota_for_virtualizor(exact_proposed_quota)

    # CRITICAL, unconditional, checked before any other status (even PREVIEW_ONLY) --
    # Virtualizor's bandwidth<=0 is empirically confirmed to mean UNLIMITED on this platform
    # (see the MANUAL_ONLY_UNLIMITED check above and writer.apply_renewal's matching guard).
    # A calculated target of exactly 0 GB (only reachable when remaining==0 AND the configured
    # gift is explicitly 0) must NEVER be written as-is -- doing so would silently make the
    # VPS unlimited, the exact opposite of a "0 GB gift" / "no further allowance" intent. This
    # is a genuine platform representation gap, not something this code can safely resolve by
    # guessing an alternative (e.g. a 1GB floor) -- that would be a NEW business rule this
    # function has no authorization to invent. Block and report instead.
    if proposed_quota is not None and proposed_quota <= 0:
        return {
            "vpsid": vpsid, "status": "BLOCKED_ZERO_QUOTA_UNSUPPORTED", "policy_source": p.source,
            "detail": f"calculated target is {proposed_quota} GB (remaining=0 and configured "
                      f"monthly_grant_gb={p.monthly_grant_gb}); Virtualizor cannot represent a "
                      f"0 GB bandwidth LIMIT distinctly from UNLIMITED (bandwidth<=0) -- this "
                      f"would NOT apply a zero allowance, it would remove the limit entirely. "
                      f"Blocked structurally. Needs an explicitly-approved decision (e.g. a "
                      f"minimum floor above 0, or manual handling) before this VPS's period "
                      f"can ever be applied.",
            "exact_calculated_quota_gb": str(exact_proposed_quota),
            "old_quota_gb": str(old_quota), "old_used_gb": str(old_used),
            "monthly_grant_gb": str(p.monthly_grant_gb),
            "overrun_gb": str(overrun_gb), "overrun_anomaly_flag": overrun_anomaly_flag,
        }

    if not p.auto_renewal_enabled:
        status = "PREVIEW_ONLY_AUTO_RENEWAL_DISABLED"
    elif eligible:
        status = "PLAUSIBLE_RENEWAL_CANDIDATE"
    else:
        status = "FROZEN_FOR_REVIEW"

    return {
        "vpsid": vpsid, "status": status, "policy_source": p.source,
        "billing_anchor": p.billing_anchor, "anchor_epoch": anchor_epoch,
        "eligibility_detail": detail,
        "old_quota_gb": str(old_quota), "old_used_gb": str(old_used),
        "monthly_grant_gb": str(p.monthly_grant_gb),
        "carryover_enabled": p.carryover_enabled,
        "carryover_cap_gb": str(p.carryover_cap_gb) if p.carryover_cap_gb is not None else "unlimited",
        "proposed_new_quota_gb": str(proposed_quota),
        "exact_calculated_quota_gb": str(exact_proposed_quota),  # pre-rounding, audit only
        "overrun_gb": str(overrun_gb), "overrun_anomaly_flag": overrun_anomaly_flag,
        "initial_grant_note": initial_grant_note,
        "auto_renewal_enabled": p.auto_renewal_enabled,
    }
