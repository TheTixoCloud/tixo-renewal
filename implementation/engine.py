"""Event-sourced, idempotent bandwidth-carryover accounting engine.

This module NEVER touches a network socket, a database, or Virtualizor. It only ever reads
normalized event/receipt objects passed to it in memory, and returns Proposal objects. All
persistence (if any) is done by the caller via storage.py, locally, to files under
implementation/ or tests/ fixtures -- never to production.

Design reference: ../analysis/phase3-design.md
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from models import Identity, Event, ReceiptRecord, Proposal, to_decimal

ZERO = Decimal(0)

UNKNOWN_CREATED = "UNKNOWN"

# Confirmed business rule (user-approved 2026-11-14, supersedes the Phase 3 "unknown monthly
# base" fail-closed placeholder): every VPS gets +100 GB base bandwidth at creation, and +100 GB
# of NEW monthly base at every monthly anniversary of its creation, on top of whatever bandwidth
# carries forward unused -- never re-adding previously purchased top-ups. This constant is the
# ONLY place that number is allowed to appear.
#
# IMPORTANT (corrects a v2 bug, per explicit review): a purchased top-up is already baked into
# `old_quota` by the time a reset happens (the admin applies it as a QUOTA_CHANGE event, which
# raises `bandwidth` directly). `remaining = old_quota - old_usage` therefore ALREADY includes
# any unused purchased credit. Adding `verified_purchase_credit` again on top would double-count
# it. Receipts are tracked and reported in every proposal's audit trail for RECONCILIATION
# ONLY -- they must never be added into `proposed_new_quota_gb`.
MONTHLY_BASE_GB = Decimal("100")
INITIAL_GRANT_GB = Decimal("100")

# How many calendar days of slack are allowed between a reset event's timestamp and the
# verified monthly-anniversary day-of-month before it is rejected as "off-anniversary." This
# is a documented, deliberately narrow assumption (not a proven business rule) to account for
# monitoring-cron lag between the actual midnight reset and when a snapshot observes it --
# NOT a license to treat an arbitrary usage drop as a renewal. See phase3-design.md addendum.
ANNIVERSARY_TOLERANCE_DAYS = 2


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _parse_aware_datetime(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp that MUST carry explicit timezone info. Returns None for
    anything unparseable or naive (no offset) -- callers must fail closed on None, never guess
    a timezone."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None  # naive timestamps are never trusted for calendar-anniversary math
    return dt


def check_monthly_anniversary(created_epoch: Optional[int], event_ts: str,
                               tolerance_days: int = ANNIVERSARY_TOLERANCE_DAYS):
    """Verify that `event_ts` falls on (within `tolerance_days` of) a true calendar
    monthly anniversary of the VPS's VERIFIED actual creation epoch -- never an inferred or
    substituted timestamp. Returns (ok: bool, detail: str).

    Fails closed (ok=False) whenever anything is unverifiable: no creation epoch at all, an
    unparseable or timezone-naive event timestamp, or an event that occurs in the same
    calendar month as creation (no month has actually elapsed yet). All date math is done in
    UTC for both sides to keep the comparison deterministic and testable; this is a documented
    convention, not a proven fact about which timezone Virtualizor's billing cycle actually
    uses -- see phase3-design.md addendum.
    """
    if created_epoch is None:
        return False, "no-verified-creation-timestamp: cannot confirm monthly-anniversary eligibility"

    event_dt = _parse_aware_datetime(event_ts)
    if event_dt is None:
        return False, "unparseable-or-naive-timestamp: cannot confirm monthly-anniversary eligibility"

    created_dt = datetime.fromtimestamp(int(created_epoch), tz=timezone.utc)
    event_utc = event_dt.astimezone(timezone.utc)
    created_utc = created_dt.astimezone(timezone.utc)

    if event_utc <= created_utc:
        return False, "event-before-creation: cannot be a monthly anniversary"

    created_month_index = created_utc.year * 12 + created_utc.month
    event_month_index = event_utc.year * 12 + event_utc.month
    if event_month_index <= created_month_index:
        return False, "same-calendar-month-as-creation: no month has elapsed yet"

    anniversary_day = min(created_utc.day, _days_in_month(event_utc.year, event_utc.month))
    anniversary_date = datetime(event_utc.year, event_utc.month, anniversary_day, tzinfo=timezone.utc)
    diff_days = abs((event_utc.date() - anniversary_date.date()).days)
    if diff_days > tolerance_days:
        return False, (f"off-anniversary: event is {diff_days} day(s) from the verified "
                        f"anniversary day ({anniversary_date.date().isoformat()}), "
                        f"tolerance is {tolerance_days}")
    return True, f"verified-anniversary: within {diff_days} day(s) of {anniversary_date.date().isoformat()}"


def renewal_period_index(created_epoch: int, event_ts: str) -> Optional[int]:
    """Elapsed whole calendar months between the verified creation epoch and `event_ts`,
    both normalized to UTC. Returns None if `event_ts` is unparseable/naive. Used to build a
    durable renewal-period identity `(vpsid, created_epoch, period_index)` so that two
    DIFFERENT events (different event_id, e.g. two slightly different monitoring snapshots of
    the same underlying reset) can never both grant a monthly base for the same period."""
    event_dt = _parse_aware_datetime(event_ts)
    if event_dt is None:
        return None
    created_utc = datetime.fromtimestamp(int(created_epoch), tz=timezone.utc)
    event_utc = event_dt.astimezone(timezone.utc)
    return (event_utc.year * 12 + event_utc.month) - (created_utc.year * 12 + created_utc.month)

# Matches the real production safety list -- but this engine computes it fresh from the
# (vpsid, reset_time) pairs it is given; it does not trust any hardcoded count.


class IdentityLedger:
    """Per-(vpsid, created) running state."""

    __slots__ = (
        "identity", "active", "last_quota", "last_used",
        "purchases", "resets", "last_reset_after_ts", "closed_reason",
    )

    def __init__(self, identity: Identity):
        self.identity = identity
        self.active = True
        self.last_quota: Optional[Decimal] = None
        self.last_used: Optional[Decimal] = None
        self.purchases: list = []  # list of (timestamp, ReceiptRecord)
        self.resets: list = []  # list of event_ids already processed for this identity
        self.last_reset_after_ts: Optional[str] = None
        self.closed_reason: Optional[str] = None

    def purchases_between(self, start_ts: Optional[str], end_ts: str) -> list:
        out = []
        for ts, rec in self.purchases:
            if (start_ts is None or ts > start_ts) and ts < end_ts:
                out.append(rec)
        return out


class CarryoverEngine:
    def __init__(self, fixed_reset_keys: set, verified_created_epochs: Optional[dict] = None,
                 historical_mode: bool = True):
        """fixed_reset_keys: set of (vpsid:int, reset_time:str) already manually fixed --
        permanently excluded from ever receiving a carryover proposal again.

        verified_created_epochs: dict vpsid(int) -> real creation unix epoch(int), sourced
        from trustworthy evidence (e.g. loader.scan_verified_created_epochs(), which reads the
        actual Virtualizor `vps.time` DB column as snapshotted in receipt files) -- NEVER from
        this engine's own VPS_ADDED ledger observation, which only proves when monitoring
        first noticed a vpsid, not when it was truly created. A vpsid missing from this dict
        has no verified eligibility and every one of its resets fails closed.

        historical_mode: when True (the default, and the ONLY mode this audit's `replay.py`
        ever uses), every `VPS_ADDED`-derived INITIAL_GRANT proposal is explicitly labeled
        `kind=INITIAL_GRANT_HISTORICAL` -- a retrospective observation from past monitoring
        data, NOT proof that a currently-live VPS is still missing its base grant. Set to
        False only for a genuinely live, real-time ingestion pipeline (which does not exist in
        this audit) where a freshly observed VPS_ADDED really is an actionable to-do right now."""
        self.fixed_reset_keys = set(fixed_reset_keys)
        self.verified_created_epochs = dict(verified_created_epochs or {})
        self.historical_mode = historical_mode
        self.ledgers: dict = {}  # vpsid -> list of IdentityLedger (chronological)
        self.processed_event_ids: set = set()
        self.proposals: list = []
        self.receipts_by_vpsid: dict = {}  # vpsid -> list[ReceiptRecord]
        self.unmatched_quota_changes: list = []  # no candidate receipt existed at all
        self.ambiguous_quota_changes: list = []  # 2+ positively-evidenced candidates, none auto-picked
        self.insufficient_evidence_quota_changes: list = []  # candidate(s) existed but lacked
        # corroborating timing/quota evidence -- never auto-matched on vpsid+amount alone
        self.granted_renewal_periods: dict = {}  # (vpsid, created_epoch, period_index) -> event_id
        self.rejected: list = []  # (event_id, reason) for anything dropped, fully logged

    # ---------- identity management ----------

    def _current_ledger(self, vpsid: int, created: str) -> IdentityLedger:
        chain = self.ledgers.setdefault(vpsid, [])
        if chain and chain[-1].active and chain[-1].identity.created == created:
            return chain[-1]
        # Either first time we see this vpsid, or identity changed (reuse) -> new ledger.
        if chain and chain[-1].active and chain[-1].identity.created != created:
            chain[-1].active = False
            chain[-1].closed_reason = "identity_changed_id_reused"
        ident = Identity(vpsid=vpsid, created=created)
        led = IdentityLedger(ident)
        chain.append(led)
        return led

    def close_vps(self, vpsid: int, created: str, reason: str = "removed"):
        chain = self.ledgers.get(vpsid)
        if not chain:
            return
        led = self._current_ledger(vpsid, created)
        led.active = False
        led.closed_reason = reason

    # ---------- ingestion ----------

    def ingest_receipt(self, rec: ReceiptRecord):
        self.receipts_by_vpsid.setdefault(rec.vpsid, []).append(rec)

    def ingest_event(self, ev: Event):
        eid = ev.event_id()
        if eid in self.processed_event_ids:
            return  # idempotent: silently skip exact duplicate re-ingestion
        self.processed_event_ids.add(eid)

        if ev.type == "VPS_ADDED":
            led = self._current_ledger(ev.identity.vpsid, ev.identity.created)
            led.last_quota = INITIAL_GRANT_GB
            led.last_used = ZERO
            led.last_reset_after_ts = ev.after_ts  # anchors the first monthly anniversary
            kind = "INITIAL_GRANT_HISTORICAL" if self.historical_mode else "INITIAL_GRANT_LIVE"
            if self.historical_mode:
                reason = ("new VPS identity observed in past monitoring evidence: the confirmed "
                          "policy is a 100 GB base grant at creation, BUT this proposal is "
                          "HISTORICAL/INFORMATIONAL -- it reflects a VPS_ADDED event already present "
                          "in old monitoring data, not a live detection of a currently un-granted VPS. "
                          "Do NOT treat this as an actionable to-do without first independently "
                          "confirming, via a live Virtualizor query, that this VPS has not already "
                          "received its base allocation by some other means in the time since.")
            else:
                reason = "new VPS identity: grant the confirmed 100 GB base allocation"
            self.proposals.append(Proposal(
                identity_key=led.identity.key(), vpsid=ev.identity.vpsid,
                created=led.identity.created, event_id=eid, reset_after_ts=ev.after_ts,
                old_quota_gb=ZERO, old_used_gb=ZERO, remaining_gb=ZERO,
                monthly_base_gb=INITIAL_GRANT_GB, verified_purchase_credit_gb=ZERO,
                contributing_receipts=[], proposed_new_quota_gb=INITIAL_GRANT_GB,
                status="INITIAL_GRANT", kind=kind, reasons=[reason],
            ))
            return
        if ev.type == "VPS_REMOVED":
            self.close_vps(ev.identity.vpsid, ev.identity.created, "removed")
            return
        if ev.type == "VPS_ID_REUSED":
            # Force-close whatever identity was previously open for this vpsid (regardless of
            # its 'created' value -- we don't need to match it, the old one is simply whatever
            # was active), then open the new identity explicitly. Never route through
            # _current_ledger with the NEW created value for the close step, or it would
            # wrongly open-then-close a throwaway ledger instead of closing the real old one.
            chain = self.ledgers.setdefault(ev.identity.vpsid, [])
            if chain and chain[-1].active:
                chain[-1].active = False
                chain[-1].closed_reason = "id_reused_before"
            self._current_ledger(ev.identity.vpsid, ev.identity.created)
            return
        if ev.type == "UNLIMITED_QUOTA_CHANGE":
            led = self._current_ledger(ev.identity.vpsid, ev.identity.created)
            led.last_quota = ev.new_quota_gb
            return

        led = self._current_ledger(ev.identity.vpsid, ev.identity.created)

        if ev.type == "QUOTA_CHANGE":
            delta = (ev.new_quota_gb or ZERO) - (ev.old_quota_gb or ZERO)
            matched, ambiguous_candidates, insufficient_candidates = self._match_receipt(
                ev.identity.vpsid, delta, ev.old_quota_gb, ev.new_quota_gb, ev.after_ts)
            led.last_quota = ev.new_quota_gb
            if matched is not None:
                led.purchases.append((matched.timestamp, matched))
            elif ambiguous_candidates:
                # Never fabricate which candidate is correct -- surface it for a human,
                # separately from the plain "no receipt at all" bucket, so a reviewer knows
                # *why* it wasn't auto-matched.
                self.ambiguous_quota_changes.append({
                    "event_id": eid, "vpsid": ev.identity.vpsid, "delta_gb": str(delta),
                    "after_ts": ev.after_ts, "status": "AMBIGUOUS_NEEDS_HUMAN_REVIEW",
                    "candidate_receipt_files": [c.source_file for c in ambiguous_candidates],
                })
            elif insufficient_candidates:
                # A same-vpsid-same-amount receipt exists, but neither its timing nor its
                # before/after quota could positively corroborate it -- missing evidence is
                # never treated as confirmation, so this is reported distinctly from both a
                # clean match and a plain "no receipt at all."
                self.insufficient_evidence_quota_changes.append({
                    "event_id": eid, "vpsid": ev.identity.vpsid, "delta_gb": str(delta),
                    "after_ts": ev.after_ts, "status": "INSUFFICIENT_EVIDENCE_NEEDS_HUMAN_REVIEW",
                    "candidate_receipt_files": [c.source_file for c in insufficient_candidates],
                })
            else:
                self.unmatched_quota_changes.append({
                    "event_id": eid, "vpsid": ev.identity.vpsid, "delta_gb": str(delta),
                    "after_ts": ev.after_ts, "status": "NEEDS_HUMAN_CLASSIFICATION",
                })
            return

        if ev.type == "USAGE_DROP_REVIEW":
            led.last_used = ev.new_used_gb
            self._process_reset(led, ev, eid)
            return

        self.rejected.append((eid, f"unknown event type {ev.type}"))

    def _match_receipt(self, vpsid: int, delta_gb: Decimal, old_quota_gb: Optional[Decimal],
                        new_quota_gb: Optional[Decimal], after_ts: str):
        """Requirement (per explicit review, twice now): do not match on vpsid+amount alone,
        and missing evidence must never be treated as confirmation. A candidate (same vpsid,
        same delta, not yet consumed) is classified as:

        - REJECTED outright if its timing or its claimed before/after quota actively
          CONTRADICTS the ledger event (definite disqualification).
        - CONFIRMED only if, after surviving the contradiction check, it has at least one
          piece of POSITIVE corroborating evidence: a comparable timestamp that is not after
          the event, or a before/after quota that actually agrees with the ledger. vpsid+amount
          alone, with no timing and no quota evidence either way, is NOT enough to confirm --
          that case is reported as "insufficient evidence," never silently matched.
        - If 2+ CONFIRMED candidates survive and are genuinely distinct (not literal copies of
          the same source file), the match is ambiguous and none is auto-picked.

        Returns (matched_receipt_or_None, ambiguous_candidates, insufficient_evidence_candidates).
        """
        candidates = [r for r in self.receipts_by_vpsid.get(vpsid, [])
                      if r.delta_gb == delta_gb and not getattr(r, "_consumed", False)]

        confirmed = []
        insufficient = []
        for r in candidates:
            timing_known = bool(r.timestamp) and bool(after_ts)
            timing_contradicts = False
            if timing_known:
                try:
                    timing_contradicts = not (r.timestamp <= after_ts)
                except TypeError:
                    timing_known = False  # incomparable formats -- treat as unknown, not confirming

            quota_known = r.before_gb is not None
            quota_contradicts = False
            if quota_known:
                if old_quota_gb is not None and r.before_gb != old_quota_gb:
                    quota_contradicts = True
                elif r.after_gb is not None and new_quota_gb is not None and r.after_gb != new_quota_gb:
                    quota_contradicts = True

            if timing_contradicts or quota_contradicts:
                continue  # actively contradicted -- reject outright, not even "insufficient"

            has_positive_evidence = (timing_known and not timing_contradicts) or \
                                     (quota_known and not quota_contradicts)
            if has_positive_evidence:
                confirmed.append(r)
            else:
                insufficient.append(r)  # vpsid+amount only -- never auto-confirmed

        if len(confirmed) == 1:
            confirmed[0]._consumed = True
            return confirmed[0], [], []
        if len(confirmed) == 0:
            return None, [], insufficient

        # 2+ confirmed survivors: if they are literal duplicates of the SAME transaction
        # record (same source_file + same timestamp -- e.g. a loader re-run accidentally
        # ingesting one receipt file twice), they are not two different purchases, just one
        # purchase seen twice. Picking either is fine and is not "guessing" between distinct
        # candidates.
        signatures = {(c.source_file, c.timestamp) for c in confirmed}
        if len(signatures) == 1:
            confirmed[0]._consumed = True
            return confirmed[0], [], []
        return None, confirmed, []  # genuinely ambiguous: distinct, positively-evidenced receipts

    def _process_reset(self, led: IdentityLedger, ev: Event, eid: str):
        vpsid = ev.identity.vpsid
        reset_key = (vpsid, ev.after_ts)

        if reset_key in self.fixed_reset_keys:
            self.proposals.append(Proposal(
                identity_key=led.identity.key(), vpsid=vpsid, created=led.identity.created,
                event_id=eid, reset_after_ts=ev.after_ts,
                old_quota_gb=ev.old_quota_gb or ZERO, old_used_gb=ev.old_used_gb or ZERO,
                remaining_gb=ZERO, monthly_base_gb=ZERO, verified_purchase_credit_gb=ZERO,
                contributing_receipts=[], proposed_new_quota_gb=ZERO, status="HISTORICALLY_EXCLUDED",
                kind="MONTHLY_RENEWAL",
                reasons=["exact (vpsid, reset_time) already in already-fixed-resets.json; never re-credited"],
            ))
            led.last_reset_after_ts = ev.after_ts
            return

        reasons = []
        old_q = ev.old_quota_gb
        new_q = ev.new_quota_gb
        old_u = ev.old_used_gb
        new_u = ev.new_used_gb

        if old_q is None or new_q is None or old_q <= 0 or new_q <= 0:
            reasons.append("unlimited or invalid quota")
        if old_q is not None and new_q is not None and old_q != new_q:
            reasons.append("quota changed during reset window; ambiguous (reset vs manual edit overlap)")
        if old_u is None or new_u is None or old_u <= new_u:
            reasons.append("usage did not decrease; not a reset pattern")
        elif old_q and old_q > 0:
            drop = old_u - new_u
            # Mirrors Guard V2's own heuristic (Phase 1): a full monthly reset drops usage to
            # ~0; a small drop is more likely a manual usage correction than a real reset.
            # Requirement #5: distinguish the two in the audit trail. Both still end up
            # FROZEN_FOR_REVIEW (neither is ever auto-applied), but the reason differs so a
            # human reviewer knows which kind of event they are looking at.
            if drop < Decimal(10) or new_u > old_u * Decimal("0.1"):
                reasons.append("usage decrease not strongly reset-like; likely a manual usage "
                                "correction rather than a monthly reset -- classify before acting")

        remaining = ZERO
        monthly_base = ZERO
        credit = ZERO
        contributing = []
        proposed = ZERO
        data_pattern_ok = not reasons

        if data_pattern_ok:
            # Confirmed formula (per explicit correction -- supersedes the v2 bug):
            #   proposed_new_quota = max(0, old_quota - old_usage) + 100
            #   new_usage = 0
            # Purchased top-ups are NEVER added here: by the time a reset happens, any unused
            # purchased credit is already baked into `old_quota` (the purchase itself was a
            # QUOTA_CHANGE that raised `bandwidth` directly). Adding `credit` again on top of
            # `remaining` would double-count the same GB twice. Receipts are still matched and
            # reported below purely for reconciliation / audit-trail visibility.
            remaining = old_q - old_u
            if remaining < 0:
                remaining = ZERO  # requirement #9: never negative credit
            monthly_base = MONTHLY_BASE_GB
            purchases = led.purchases_between(led.last_reset_after_ts, ev.after_ts)
            credit = sum((p.delta_gb for p in purchases), ZERO)
            contributing = [{"source_file": p.source_file, "amount_gb": str(p.delta_gb), "timestamp": p.timestamp} for p in purchases]
            proposed = remaining + monthly_base  # credit intentionally excluded -- see above
            if proposed < 0:
                reasons.append("computed proposal negative; rejected")
                data_pattern_ok = not reasons

        eligible = False
        eligibility_detail = None
        verified_epoch = None
        period_key = None
        if data_pattern_ok:
            # Renewal eligibility requires the VERIFIED actual creation timestamp and a real
            # calendar-month anniversary -- a usage drop alone is never sufficient, and an
            # unverifiable/inferred creation date must fail closed rather than assume safety.
            #
            # IMPORTANT CAVEAT (per explicit review): passing this check proves the event is
            # *consistent with* a monthly anniversary pattern. It is NOT independent proof
            # that Virtualizor itself actually triggered a billing-cycle renewal at that
            # moment -- a coincidental manual admin edit landing near the same calendar day
            # would pass this exact same check. See analysis/authoritative-evidence-needed.md
            # for what additional Virtualizor-side evidence (e.g. a billing/cron log entry, a
            # `nextresetdate`-style column) would be needed to upgrade this from "plausible
            # candidate" to "confirmed system-triggered renewal." This engine deliberately
            # never claims the stronger thing.
            verified_epoch = self.verified_created_epochs.get(vpsid)
            eligible, eligibility_detail = check_monthly_anniversary(verified_epoch, ev.after_ts)
            if not eligible:
                reasons.append(eligibility_detail)

        duplicate_period_of = None
        if data_pattern_ok and eligible:
            period_index = renewal_period_index(verified_epoch, ev.after_ts)
            period_key = f"{vpsid}|{verified_epoch}|{period_index}"
            prior_eid = self.granted_renewal_periods.get(period_key)
            if prior_eid is not None and prior_eid != eid:
                # Requirement #1: a DIFFERENT event_id (e.g. a slightly different monitoring
                # snapshot of the same underlying reset) must never grant a second monthly
                # base for a period that was already granted. This is distinct from the plain
                # ingest_event() event_id dedup above, which only catches byte-identical
                # re-ingestion -- this catches two non-identical events landing in the same
                # durable (vpsid, created_epoch, period_index) renewal period.
                duplicate_period_of = prior_eid
            else:
                self.granted_renewal_periods[period_key] = eid

        if data_pattern_ok and eligible and duplicate_period_of is None:
            status = "READY_FOR_HUMAN_APPLICATION"
            kind = "MONTHLY_RENEWAL"
            reasons = ["PLAUSIBLE monthly-renewal candidate: formula applied (max(0, old_quota-old_usage) "
                       "+ 100 GB base), calendar-anniversary pattern verified, renewal period not "
                       "previously granted, receipts shown for reconciliation only and never added to "
                       "the proposal. This is NOT independent proof Virtualizor itself triggered a "
                       "renewal -- see analysis/authoritative-evidence-needed.md. This engine never "
                       "writes to Virtualizor -- a human must independently confirm and apply this "
                       "value via the admin panel",
                       eligibility_detail]
        elif data_pattern_ok and eligible and duplicate_period_of is not None:
            status = "REJECTED_duplicate-renewal-period"
            kind = "DUPLICATE_RENEWAL_PERIOD"
            reasons = [f"duplicate-renewal-period: event {eid} falls in the same (vpsid, created_epoch, "
                       f"period) as already-granted event {duplicate_period_of}; a monthly base must "
                       f"never be granted twice for one period regardless of differing event_id"]
        elif data_pattern_ok:
            # Data pattern looks like a reset, numbers are computed and shown for audit
            # transparency, but eligibility could not be verified -- fail closed, never
            # auto-ready. `kind` is distinct from a plain data-shape rejection so a reviewer
            # can tell "this looks like a reset but we can't confirm WHEN it's allowed" apart
            # from "this doesn't even look like a reset."
            status = f"REJECTED_{'_'.join(r.split()[0] for r in reasons)}"
            kind = "UNVERIFIED_ELIGIBILITY"
        else:
            status = f"REJECTED_{'_'.join(r.split()[0] for r in reasons)}"
            kind = "NOT_A_RENEWAL"

        self.proposals.append(Proposal(
            identity_key=led.identity.key(), vpsid=vpsid, created=led.identity.created,
            event_id=eid, reset_after_ts=ev.after_ts,
            old_quota_gb=old_q or ZERO, old_used_gb=old_u or ZERO,
            remaining_gb=remaining, monthly_base_gb=monthly_base, verified_purchase_credit_gb=credit,
            contributing_receipts=contributing, proposed_new_quota_gb=proposed,
            status=status, kind=kind, reasons=reasons,
            renewal_period_key=period_key if (data_pattern_ok and eligible and duplicate_period_of is None) else None,
        ))
        led.last_reset_after_ts = ev.after_ts

    # ---------- output ----------

    def get_proposals(self):
        return list(self.proposals)

    def get_unmatched_quota_changes(self):
        return list(self.unmatched_quota_changes)

    def get_ambiguous_quota_changes(self):
        return list(self.ambiguous_quota_changes)

    def get_insufficient_evidence_quota_changes(self):
        return list(self.insufficient_evidence_quota_changes)

    def snapshot_state(self) -> dict:
        """Serializable snapshot for crash-recovery tests. Decimal-safe."""
        return {
            "processed_event_ids": sorted(self.processed_event_ids),
            "fixed_reset_keys": sorted(f"{v}|{t}" for v, t in self.fixed_reset_keys),
            "ledgers": {
                str(vpsid): [
                    {
                        "created": led.identity.created,
                        "active": led.active,
                        "last_quota": str(led.last_quota) if led.last_quota is not None else None,
                        "last_used": str(led.last_used) if led.last_used is not None else None,
                        "last_reset_after_ts": led.last_reset_after_ts,
                        "closed_reason": led.closed_reason,
                        "purchases": [[ts, rec.delta_gb.__str__()] for ts, rec in led.purchases],
                    }
                    for led in chain
                ]
                for vpsid, chain in self.ledgers.items()
            },
            "proposals": [p.to_json() for p in self.proposals],
            "unmatched_quota_changes": self.unmatched_quota_changes,
            "ambiguous_quota_changes": self.ambiguous_quota_changes,
            "insufficient_evidence_quota_changes": self.insufficient_evidence_quota_changes,
        }
