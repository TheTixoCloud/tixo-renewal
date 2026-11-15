"""Comprehensive test suite for the local, never-deployed bandwidth carryover engine.

Every test operates purely in memory against synthetic fixtures -- no network, no SSH, no
production access of any kind happens in this file.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

from engine import CarryoverEngine, check_monthly_anniversary
from models import Identity, Event, ReceiptRecord
from storage import atomic_write_json, read_json
from loader import (load_receipts, build_created_lookup, load_ledger_events_with_identity_anchors,
                     scan_verified_created_epochs)

TZ = timezone(timedelta(hours=3, minutes=30))  # Asia/Tehran-equivalent fixed offset, test-only


def epoch(year, month, day, hour=12, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp())


def iso(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ).isoformat()


def ev(type_, vpsid, created, before, after, old_q=None, new_q=None, old_u=None, new_u=None):
    def d(x):
        return Decimal(str(x)) if x is not None else None
    return Event(
        type=type_, identity=Identity(vpsid=vpsid, created=created),
        before_ts=before, after_ts=after,
        old_quota_gb=d(old_q), new_quota_gb=d(new_q),
        old_used_gb=d(old_u), new_used_gb=d(new_u),
    )


def receipt(vpsid, delta, ts, fname="fixture.json"):
    return ReceiptRecord(vpsid=vpsid, delta_gb=Decimal(str(delta)), timestamp=ts, source_file=fname, detail="test")


# 1. Initial balance -------------------------------------------------------

def test_initial_balance_empty_engine():
    eng = CarryoverEngine(fixed_reset_keys=set())
    assert eng.get_proposals() == []
    assert eng.ledgers == {}


def test_initial_balance_vps_added_grants_100gb_base():
    """Confirmed business rule: every new VPS identity gets a 100 GB base grant at creation."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("VPS_ADDED", 1, "100", "T0", "T1"))
    led = eng.ledgers[1][-1]
    assert led.active is True
    assert led.last_quota == Decimal("100")
    p = eng.get_proposals()[0]
    assert p.status == "INITIAL_GRANT"
    assert p.kind == "INITIAL_GRANT_HISTORICAL"  # default historical_mode=True -- never an actionable to-do
    assert p.proposed_new_quota_gb == Decimal("100")


def test_initial_grant_live_mode_is_distinct_from_historical():
    """historical_mode=False (a genuinely live ingestion pipeline, which this audit never
    runs) must label the grant differently so it is never confused with a replay-derived
    historical/informational entry."""
    eng = CarryoverEngine(fixed_reset_keys=set(), historical_mode=False)
    eng.ingest_event(ev("VPS_ADDED", 1, "100", "T0", "T1"))
    p = eng.get_proposals()[0]
    assert p.kind == "INITIAL_GRANT_LIVE"


def test_replay_py_always_uses_historical_mode():
    """Regression guard: replay.py (the only driver this audit ever runs) must always
    construct the engine with historical_mode=True, since it only ever reads past evidence."""
    import inspect
    import replay
    source = inspect.getsource(replay.run)
    assert "historical_mode=True" in source


# 2. Normal consumption (usage rising, no drop) ----------------------------

def test_normal_consumption_is_not_a_reset():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("VPS_ADDED", 1, "100", "T0", "T1"))
    eng.ingest_event(ev("QUOTA_CHANGE", 1, "100", "T1", "T2", old_q=1000, new_q=1000))
    # Usage going UP is never represented as USAGE_DROP_REVIEW by the real production ledger
    # (it only logs drops) -- if it ever were, the engine must reject it as "not a reset".
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 1, "100", "T2", "T3", old_q=1000, new_q=1000, old_u=10, new_u=50))
    p = eng.get_proposals()[1]  # [0] is the VPS_ADDED INITIAL_GRANT, [1] is this event's result
    assert p.status.startswith("REJECTED")
    assert "did not decrease" in p.reasons[0]


# 3. Monthly reset ----------------------------------------------------------

def test_monthly_reset_no_prior_purchases():
    """new_quota = max(0, old_quota - old_usage) + 100 GB monthly base. Eligibility requires
    a VERIFIED real creation epoch and a true calendar-month anniversary -- both supplied
    here with real, aligned dates."""
    created_epoch = epoch(2026, 9, 15)
    eng = CarryoverEngine(fixed_reset_keys=set(), verified_created_epochs={1: created_epoch})
    eng.ingest_event(ev("VPS_ADDED", 1, "100", "T0", iso(2026, 9, 15)))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 1, "100", iso(2026, 9, 15), iso(2026, 10, 15),
                         old_q=1000, new_q=1000, old_u=900, new_u=0.5))
    proposals = eng.get_proposals()
    assert proposals[0].status == "INITIAL_GRANT"  # from VPS_ADDED
    p = proposals[1]
    assert p.status == "READY_FOR_HUMAN_APPLICATION"
    assert p.kind == "MONTHLY_RENEWAL"
    assert p.remaining_gb == Decimal("100")
    assert p.monthly_base_gb == Decimal("100")
    assert p.verified_purchase_credit_gb == Decimal("0")
    assert p.proposed_new_quota_gb == Decimal("200")


def test_user_example_8100_minus_5000_plus_100_equals_3200():
    """The exact worked example confirmed by the business: 8100 GB quota, 5000 GB used,
    reset to 3200 GB (= (8100-5000) + 100). Verified creation epoch + aligned anniversary
    date supplied so the proposal reaches READY_FOR_HUMAN_APPLICATION."""
    created_epoch = epoch(2026, 9, 14)
    eng = CarryoverEngine(fixed_reset_keys=set(), verified_created_epochs={50: created_epoch})
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 50, "created-50", iso(2026, 10, 1), iso(2026, 10, 14),
                         old_q=8100, new_q=8100, old_u=5000, new_u=1))
    p = eng.get_proposals()[0]
    assert p.status == "READY_FOR_HUMAN_APPLICATION"
    assert p.remaining_gb == Decimal("3100")
    assert p.monthly_base_gb == Decimal("100")
    assert p.verified_purchase_credit_gb == Decimal("0")
    assert p.proposed_new_quota_gb == Decimal("3200")


# 4. Duplicate reset (exact same event re-ingested) -------------------------

def test_duplicate_reset_event_is_idempotent():
    eng = CarryoverEngine(fixed_reset_keys=set())
    e = ev("USAGE_DROP_REVIEW", 2, "200", "T1", "T2", old_q=500, new_q=500, old_u=400, new_u=1)
    eng.ingest_event(e)
    eng.ingest_event(e)  # identical event re-ingested
    assert len(eng.get_proposals()) == 1


# 5. Multiple consecutive monthly resets -------------------------------------

def test_multiple_consecutive_resets_no_purchase_between():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 3, "300", "T1", "T2", old_q=1000, new_q=1000, old_u=800, new_u=1))
    # Admin applies the first renewal's proposed_new_quota_gb (200 remaining + 100 base = 300)
    # before the next reset happens.
    eng.ingest_event(ev("QUOTA_CHANGE", 3, "300", "T2", "T2a", old_q=1000, new_q=300))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 3, "300", "T3", "T4", old_q=300, new_q=300, old_u=150, new_u=1))
    props = eng.get_proposals()
    assert len(props) == 2
    assert props[0].remaining_gb == Decimal("200")
    assert props[0].proposed_new_quota_gb == Decimal("300")
    assert props[1].remaining_gb == Decimal("150")
    assert props[1].monthly_base_gb == Decimal("100")
    assert props[1].verified_purchase_credit_gb == Decimal("0")
    assert props[1].proposed_new_quota_gb == Decimal("250")


def test_multi_month_carryover_five_consecutive_resets_no_purchases():
    """The 100 GB base must be added exactly once per renewal, every month, indefinitely,
    and never re-granted retroactively for a month that already received it."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    vpsid, created = 60, "created-60"
    quota = Decimal("1000")
    used_each_month = Decimal("50")  # customer uses only 50 GB/month -> carryover should grow
    expected_quota_after = []
    t = 1
    for month in range(5):
        before_ts, after_ts = f"T{t}", f"T{t+1}"
        t += 2
        eng.ingest_event(ev("USAGE_DROP_REVIEW", vpsid, created, before_ts, after_ts,
                             old_q=quota, new_q=quota, old_u=used_each_month, new_u=1))
        remaining = quota - used_each_month
        quota = remaining + Decimal("100")  # this is what the admin would apply before next month
        expected_quota_after.append(quota)

    props = eng.get_proposals()
    assert len(props) == 5
    # Month 1: 1000 - 50 + 100 = 1050; month 2: 1050-50+100=1100; ... grows by 50 net each month.
    assert [p.proposed_new_quota_gb for p in props] == expected_quota_after
    assert expected_quota_after == [Decimal("1050"), Decimal("1100"), Decimal("1150"),
                                     Decimal("1200"), Decimal("1250")]
    assert all(p.monthly_base_gb == Decimal("100") for p in props)
    assert all(p.verified_purchase_credit_gb == Decimal("0") for p in props)


# 6. Purchased top-up ---------------------------------------------------------

def test_purchased_topup_is_credited_at_next_reset():
    """CORRECTED formula (per explicit review): proposed = remaining + 100 GB base ONLY.
    A purchased top-up is already baked into `old_quota` by the time the reset happens (the
    QUOTA_CHANGE event that applied it already raised `bandwidth` directly), so
    `remaining = old_quota - old_usage` already reflects the unused purchased GB. The receipt
    is still matched and reported in `verified_purchase_credit_gb` / `contributing_receipts`
    for reconciliation/audit purposes ONLY -- it must NOT be added again into
    `proposed_new_quota_gb`, or the customer's purchase is double-counted."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(4, 500, "T1.5"))
    eng.ingest_event(ev("QUOTA_CHANGE", 4, "400", "T1", "T2", old_q=1000, new_q=1500))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 4, "400", "T2", "T3", old_q=1500, new_q=1500, old_u=1400, new_u=1))
    p = eng.get_proposals()[0]
    assert p.verified_purchase_credit_gb == Decimal("500")  # audit trail only
    assert p.remaining_gb == Decimal("100")
    assert p.monthly_base_gb == Decimal("100")
    assert p.proposed_new_quota_gb == Decimal("200")  # NOT 700 -- the 500 is already in old_quota
    assert len(p.contributing_receipts) == 1


def test_purchase_credit_never_added_to_proposed_quota_regression():
    """Regression test that specifically fails on the v2 double-counting bug: even with a
    REAL, matched 500 GB purchase credit on record (verified_purchase_credit_gb == 500, same
    as test_purchased_topup_is_credited_at_next_reset), the proposed quota must equal
    remaining+100 and NOT remaining+100+credit. Asserts both the pre-fix (wrong) and
    post-fix (correct) values explicitly so a regression is unmistakable."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(2, 500, "T1.5"))
    eng.ingest_event(ev("QUOTA_CHANGE", 2, "200", "T1", "T2", old_q=1000, new_q=1500))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 2, "200", "T2", "T3", old_q=1500, new_q=1500, old_u=1400, new_u=1))
    p = eng.get_proposals()[0]
    assert p.verified_purchase_credit_gb == Decimal("500")  # the credit WAS matched...
    wrong_v2_value = p.remaining_gb + p.monthly_base_gb + p.verified_purchase_credit_gb  # 700
    correct_value = p.remaining_gb + p.monthly_base_gb  # 200
    assert wrong_v2_value == Decimal("700")
    assert correct_value == Decimal("200")
    assert p.proposed_new_quota_gb == correct_value  # ...but never added into the proposal
    assert p.proposed_new_quota_gb != wrong_v2_value


# 7. Duplicate purchase receipt ------------------------------------------------

def test_duplicate_purchase_receipt_not_double_counted():
    eng = CarryoverEngine(fixed_reset_keys=set())
    # Same receipt ingested twice (e.g. loader re-run on the same file) must not grant 1000.
    eng.ingest_receipt(receipt(5, 500, "T1.5", fname="r.json"))
    eng.ingest_receipt(receipt(5, 500, "T1.5", fname="r.json"))
    eng.ingest_event(ev("QUOTA_CHANGE", 5, "500", "T1", "T2", old_q=1000, new_q=1500))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 5, "500", "T2", "T3", old_q=1500, new_q=1500, old_u=1400, new_u=1))
    p = eng.get_proposals()[0]
    assert p.verified_purchase_credit_gb == Decimal("500")  # only ONE quota-increase event existed
    assert len(eng.receipts_by_vpsid[5]) == 2  # both receipts loaded, but only one was consumable


# 8. Purchase correction -------------------------------------------------------

def test_purchase_correction_reduces_net_credit():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(6, 500, "T1.1"))
    eng.ingest_receipt(receipt(6, -250, "T1.3"))  # "accidental +500, corrected to +250"
    eng.ingest_event(ev("QUOTA_CHANGE", 6, "600", "T1", "T1.2", old_q=1000, new_q=1500))
    eng.ingest_event(ev("QUOTA_CHANGE", 6, "600", "T1.2", "T1.4", old_q=1500, new_q=1250))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 6, "600", "T1.4", "T2", old_q=1250, new_q=1250, old_u=1200, new_u=1))
    p = eng.get_proposals()[0]
    assert p.verified_purchase_credit_gb == Decimal("250")


# 9. Quota decrease with no receipt (unexplained) ------------------------------

def test_unexplained_quota_decrease_is_flagged_not_negative_credit():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("QUOTA_CHANGE", 7, "700", "T1", "T2", old_q=1000, new_q=800))
    assert len(eng.get_unmatched_quota_changes()) == 1
    assert eng.get_unmatched_quota_changes()[0]["status"] == "NEEDS_HUMAN_CLASSIFICATION"
    led = eng.ledgers[7][-1]
    assert led.last_quota == Decimal("800")  # observed fact recorded, no credit fabricated


# 10. Usage correction distinguished from a real reset -------------------------

def test_small_usage_drop_is_classified_as_correction_not_reset():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 8, "800", "T1", "T2", old_q=1000, new_q=1000, old_u=50.5, new_u=50.0))
    p = eng.get_proposals()[0]
    assert p.status.startswith("REJECTED")
    assert any("manual usage correction" in r for r in p.reasons)


def test_large_usage_drop_alone_is_insufficient_without_verified_anniversary():
    """Regression test for the v2/review gap: a usage drop that LOOKS exactly like a clean
    monthly reset must still fail closed when there is no verified actual creation timestamp
    to confirm it really falls on a calendar-month anniversary. No `verified_created_epochs`
    is supplied here on purpose."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 9, "900", "T1", "T2", old_q=1000, new_q=1000, old_u=900, new_u=1))
    p = eng.get_proposals()[0]
    assert p.status != "READY_FOR_HUMAN_APPLICATION"
    assert p.status.startswith("REJECTED")
    assert p.kind == "UNVERIFIED_ELIGIBILITY"
    assert "no-verified-creation-timestamp" in p.reasons[0]
    # The numbers are still computed and shown for audit transparency, just not applied.
    assert p.remaining_gb == Decimal("100")
    assert p.proposed_new_quota_gb == Decimal("200")


def test_large_usage_drop_becomes_eligible_once_anniversary_is_verified():
    """Same usage-drop shape as above, but now WITH a verified creation epoch whose calendar
    anniversary aligns with the event timestamp -- this is the only way it may become
    READY_FOR_HUMAN_APPLICATION."""
    created_epoch = epoch(2026, 9, 9)
    eng = CarryoverEngine(fixed_reset_keys=set(), verified_created_epochs={9: created_epoch})
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 9, "900", iso(2026, 9, 9), iso(2026, 10, 9),
                         old_q=1000, new_q=1000, old_u=900, new_u=1))
    p = eng.get_proposals()[0]
    assert p.status == "READY_FOR_HUMAN_APPLICATION"
    assert p.kind == "MONTHLY_RENEWAL"


# 11. Unlimited quota -----------------------------------------------------------

def test_unlimited_quota_reset_is_rejected():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 10, "1000", "T1", "T2", old_q=0, new_q=0, old_u=900, new_u=1))
    p = eng.get_proposals()[0]
    assert p.status.startswith("REJECTED")
    assert any("unlimited" in r for r in p.reasons)


# 12. Over-limit consumption: never negative credit ------------------------------

def test_over_limit_consumption_never_negative_remaining():
    eng = CarryoverEngine(fixed_reset_keys=set())
    # VPS 76-style case: used slightly exceeds quota (old_used > old_quota).
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 11, "1100", "T1", "T2", old_q=100, new_q=100, old_u=100.06, new_u=0.01))
    p = eng.get_proposals()[0]
    assert p.remaining_gb == Decimal("0")
    # Even with zero carryover, the confirmed 100 GB monthly base is still granted.
    assert p.proposed_new_quota_gb == Decimal("100")


# 13. VPS deletion ----------------------------------------------------------------

def test_vps_deletion_closes_ledger():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("VPS_ADDED", 12, "1200", "T0", "T1"))
    eng.ingest_event(ev("VPS_REMOVED", 12, "1200", "T1", "T2"))
    led = eng.ledgers[12][-1]
    assert led.active is False
    assert led.closed_reason == "removed"


# 14. VPS ID reuse: credit never transfers across identities ----------------------

def test_id_reuse_does_not_transfer_credit():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(13, 500, "T1.5"))
    eng.ingest_event(ev("VPS_ADDED", 13, "OLD_CREATED", "T0", "T1"))
    eng.ingest_event(ev("QUOTA_CHANGE", 13, "OLD_CREATED", "T1", "T2", old_q=1000, new_q=1500))
    # Same integer vpsid, new created timestamp -> a different customer's VPS now.
    eng.ingest_event(ev("VPS_ID_REUSED", 13, "NEW_CREATED", "T2", "T3"))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 13, "NEW_CREATED", "T3", "T4", old_q=200, new_q=200, old_u=190, new_u=1))
    chain = eng.ledgers[13]
    assert len(chain) == 2
    assert chain[0].active is False
    assert chain[0].closed_reason == "id_reused_before"
    p = [p for p in eng.get_proposals() if p.created == "NEW_CREATED"][0]
    assert p.verified_purchase_credit_gb == Decimal("0")  # old identity's 500 GB purchase did not follow


# 15. Missing monitoring snapshot (gap in event stream) ---------------------------

def test_missing_snapshot_gap_does_not_crash_engine():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("QUOTA_CHANGE", 14, "1400", "T1", "T2", old_q=1000, new_q=1200))
    # Large time gap simulating a missed snapshot window; engine has no contiguity requirement.
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 14, "1400", "T50", "T51", old_q=1200, new_q=1200, old_u=1100, new_u=1))
    assert len(eng.get_proposals()) == 1


# 16. Unexpected quota increase with no receipt -----------------------------------

def test_unexpected_quota_increase_needs_human_classification():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_event(ev("QUOTA_CHANGE", 15, "1500", "T1", "T2", old_q=1473, new_q=2473))  # the real VPS-4 pattern
    unmatched = eng.get_unmatched_quota_changes()
    assert len(unmatched) == 1
    assert unmatched[0]["delta_gb"] == "1000"
    assert unmatched[0]["status"] == "NEEDS_HUMAN_CLASSIFICATION"


# 17. Interrupted transaction (crash mid-write) -----------------------------------

def test_atomic_write_survives_leftover_tmp_from_a_previous_crash(tmp_path):
    target = tmp_path / "state.json"
    leftover_tmp = tmp_path / "state.json.tmp"
    leftover_tmp.write_text("{CORRUPTED, half-written data from a simulated crash")
    atomic_write_json(target, {"ok": True, "n": 42})
    assert read_json(target) == {"ok": True, "n": 42}
    assert not leftover_tmp.exists() or leftover_tmp.read_text() != "{CORRUPTED, half-written data from a simulated crash"


def test_atomic_write_never_leaves_partial_target(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"v": 1})
    # Simulate a second writer crashing before os.replace: tmp file written, but target untouched.
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text("GARBAGE")
    # The real target must still be readable and correct -- a crash between tmp-write and
    # replace never corrupts the file readers actually see.
    assert read_json(target) == {"v": 1}


# 18. Replay from the beginning ----------------------------------------------------

def test_full_replay_from_empty_state():
    eng = CarryoverEngine(fixed_reset_keys={(99, "T_FIXED")})
    eng.ingest_event(ev("VPS_ADDED", 20, "2000", "T0", "T1"))
    eng.ingest_receipt(receipt(20, 300, "T1.5"))
    eng.ingest_event(ev("QUOTA_CHANGE", 20, "2000", "T1", "T2", old_q=1000, new_q=1300))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 20, "2000", "T2", "T3", old_q=1300, new_q=1300, old_u=1250, new_u=1))
    proposals = eng.get_proposals()
    assert len(proposals) == 2  # INITIAL_GRANT (VPS_ADDED) + MONTHLY_RENEWAL (USAGE_DROP_REVIEW)
    assert proposals[0].status == "INITIAL_GRANT"
    assert proposals[0].proposed_new_quota_gb == Decimal("100")
    assert proposals[1].verified_purchase_credit_gb == Decimal("300")  # audit trail only
    assert proposals[1].proposed_new_quota_gb == Decimal("150")  # 50 remaining + 100 base, no credit added


# 19. Idempotent replay: running the whole thing twice gives the same result --------

def build_sample_events():
    return [
        ev("VPS_ADDED", 21, "2100", "T0", "T1"),
        ev("QUOTA_CHANGE", 21, "2100", "T1", "T2", old_q=1000, new_q=1300),
        ev("USAGE_DROP_REVIEW", 21, "2100", "T2", "T3", old_q=1300, new_q=1300, old_u=1250, new_u=1),
    ]


def test_idempotent_replay_same_engine_twice():
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(21, 300, "T1.5"))
    events = build_sample_events()
    for e in events:
        eng.ingest_event(e)
    first = [p.to_json() for p in eng.get_proposals()]
    for e in events:  # replay the exact same events again on the SAME engine
        eng.ingest_event(e)
    second = [p.to_json() for p in eng.get_proposals()]
    assert first == second  # no duplicate proposals were created


def test_idempotent_replay_two_independent_engines_match():
    def run_once():
        eng = CarryoverEngine(fixed_reset_keys=set())
        eng.ingest_receipt(receipt(21, 300, "T1.5"))
        for e in build_sample_events():
            eng.ingest_event(e)
        return [p.to_json() for p in eng.get_proposals()]

    assert run_once() == run_once()


# 20. Historical already-fixed events must never be re-credited ---------------------

def test_historically_fixed_reset_is_excluded_even_with_purchase_credit_available():
    fixed = {(22, "T2")}
    eng = CarryoverEngine(fixed_reset_keys=fixed)
    eng.ingest_receipt(receipt(22, 9999, "T1.5"))  # even a huge purchase must not leak in
    eng.ingest_event(ev("QUOTA_CHANGE", 22, "2200", "T1", "T1.5", old_q=1000, new_q=10999))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 22, "2200", "T1.5", "T2", old_q=10999, new_q=10999, old_u=10000, new_u=1))
    p = eng.get_proposals()[0]
    assert p.status == "HISTORICALLY_EXCLUDED"
    assert p.proposed_new_quota_gb == Decimal("0")


# 21. Recovery from a crash: event-sourced replay is the recovery mechanism ---------

def test_crash_recovery_is_just_a_clean_full_replay():
    """The engine holds no durable state of its own between runs (Phase 4 design: a pure
    in-memory replay of the append-only evidence files). 'Crash recovery' for this system
    therefore reduces to re-running the full replay from the untouched event log -- which
    test 19 already proves is idempotent and deterministic. This test exercises that exact
    narrative: simulate a crash partway through ingestion, 'restart' with a fresh engine,
    and confirm the full re-run from scratch reaches the same final state as an
    uninterrupted run.
    """
    events = build_sample_events()

    eng_crashed = CarryoverEngine(fixed_reset_keys=set())
    eng_crashed.ingest_receipt(receipt(21, 300, "T1.5"))
    for e in events[:2]:  # crash after 2 of 3 events (VPS_ADDED + QUOTA_CHANGE)
        eng_crashed.ingest_event(e)
    crashed_proposals = eng_crashed.get_proposals()
    assert len(crashed_proposals) == 1
    assert crashed_proposals[0].status == "INITIAL_GRANT"  # only the creation grant so far
    assert not any(p.kind == "MONTHLY_RENEWAL" for p in crashed_proposals)  # reset not processed yet

    # "Restart": fresh engine, full event log replayed from the beginning (not resumed from
    # the crashed engine's partial state -- this is the safe, auditable recovery model).
    eng_recovered = CarryoverEngine(fixed_reset_keys=set())
    eng_recovered.ingest_receipt(receipt(21, 300, "T1.5"))
    for e in events:
        eng_recovered.ingest_event(e)

    uninterrupted = CarryoverEngine(fixed_reset_keys=set())
    uninterrupted.ingest_receipt(receipt(21, 300, "T1.5"))
    for e in events:
        uninterrupted.ingest_event(e)

    assert [p.to_json() for p in eng_recovered.get_proposals()] == [p.to_json() for p in uninterrupted.get_proposals()]


# 22. Duplicate monthly renewal (exact same reset re-ingested) must not double-grant +100 ------

def test_duplicate_monthly_renewal_event_grants_100gb_exactly_once():
    eng = CarryoverEngine(fixed_reset_keys=set())
    e = ev("USAGE_DROP_REVIEW", 61, "created-61", "T1", "T2", old_q=1000, new_q=1000, old_u=900, new_u=1)
    eng.ingest_event(e)
    eng.ingest_event(e)  # the exact same ledger line re-ingested (e.g. a re-run of the loader)
    eng.ingest_event(e)  # and again
    proposals = eng.get_proposals()
    assert len(proposals) == 1
    assert proposals[0].monthly_base_gb == Decimal("100")
    assert proposals[0].proposed_new_quota_gb == Decimal("200")  # NOT 400 or 300


# 23. Crash recovery across a multi-month sequence (interrupted after month 2 of 3) ------------

def build_multi_month_events(vpsid=70, created="created-70"):
    return [
        ev("VPS_ADDED", vpsid, created, "T0", "T1"),
        ev("USAGE_DROP_REVIEW", vpsid, created, "T1", "T2", old_q=100, new_q=100, old_u=60, new_u=1),   # ->140
        ev("USAGE_DROP_REVIEW", vpsid, created, "T2", "T3", old_q=140, new_q=140, old_u=90, new_u=1),   # ->150
        ev("USAGE_DROP_REVIEW", vpsid, created, "T3", "T4", old_q=150, new_q=150, old_u=70, new_u=1),   # ->180
    ]


def test_crash_recovery_mid_multi_month_sequence_matches_uninterrupted_run():
    events = build_multi_month_events()

    crashed = CarryoverEngine(fixed_reset_keys=set())
    for e in events[:3]:  # crash after month 2's renewal, before month 3's
        crashed.ingest_event(e)
    assert len(crashed.get_proposals()) == 3  # INITIAL_GRANT + 2 renewals

    recovered = CarryoverEngine(fixed_reset_keys=set())
    for e in events:  # full replay from scratch, per the crash-recovery model
        recovered.ingest_event(e)

    uninterrupted = CarryoverEngine(fixed_reset_keys=set())
    for e in events:
        uninterrupted.ingest_event(e)

    recovered_json = [p.to_json() for p in recovered.get_proposals()]
    uninterrupted_json = [p.to_json() for p in uninterrupted.get_proposals()]
    assert recovered_json == uninterrupted_json
    assert len(recovered_json) == 4
    assert recovered_json[-1]["proposed_new_quota_gb"] == "180"  # (150-70)+100 = 180


# 24. Receipt loader: malformed JSON is reported, never silently dropped -----------------------

def test_malformed_receipt_file_is_reported_not_silently_skipped(tmp_path):
    (tmp_path / "good.json").write_text('{"vpsid": 1, "added": 500, "time": "T1"}')
    (tmp_path / "broken.json").write_text('{"vpsid": 1, "added": 500,')  # truncated, invalid JSON
    receipts, malformed, skipped_backups = load_receipts(str(tmp_path))
    assert len(receipts) == 1
    assert receipts[0].delta_gb == Decimal("500")
    assert len(malformed) == 1
    assert malformed[0]["file"] == "broken.json"
    assert skipped_backups == []


# 25. Receipt loader: a file that is only a "backup" snapshot of another receipt is excluded ---

def test_backup_snapshot_receipt_is_not_double_counted(tmp_path):
    """Reproduces a real bug found in production evidence: a `*-before-*` file that is
    referenced via another file's `"backup"` field carries the SAME add-amount in its own
    `targets` block. Counting both as independent receipts would double-credit one real
    purchase."""
    main = tmp_path / "topup-main.json"
    backup = tmp_path / "topup-main-before.json"
    main.write_text(json.dumps({
        "created_at": "T1", "total_added_gb": 1800,
        "backup": str(backup),
        "items": [{"vpsid": 35, "added_gb": 1800}],
    }))
    backup.write_text(json.dumps({
        "created_at": "T1",
        "targets": {"1.2.3.4": [35, 18659, 1800]},  # same vpsid, same 1800 amount
    }))
    receipts, malformed, skipped_backups = load_receipts(str(tmp_path))
    assert len(malformed) == 0
    assert skipped_backups == ["topup-main-before.json"]
    assert len(receipts) == 1  # not 2 -- the backup file's duplicate 1800 must not be counted
    assert receipts[0].delta_gb == Decimal("1800")
    assert receipts[0].source_file == "topup-main.json"


# 26. Identity anchoring: VPS_ADDED timestamp replaces the shared UNKNOWN_CREATED constant ------

def test_identity_anchor_distinguishes_reused_vpsid_after_monitoring_started(tmp_path):
    """Without an anchor, two different real-world customers who both got vpsid=99 (old one
    removed, new one created later) would collapse into a single UNKNOWN_CREATED identity and
    could leak credit across customers. With the anchor, the second VPS_ADDED gives the new
    identity a distinct `created` value derived from its own ledger line."""
    ledger_path = tmp_path / "ledger-events.jsonl"
    lines = [
        {"type": "VPS_ADDED", "vpsid": 99, "before": "T0", "after": "T1"},
        {"type": "VPS_REMOVED", "vpsid": 99, "before": "T1", "after": "T2"},
        {"type": "VPS_ADDED", "vpsid": 99, "before": "T2", "after": "T3"},  # id reused
    ]
    ledger_path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    anchored = load_ledger_events_with_identity_anchors(str(ledger_path))
    created_values = {e.identity.created for e in anchored if e.type == "VPS_ADDED"}
    assert created_values == {"T1", "T3"}  # two distinct identities, not one shared UNKNOWN


# 27. check_monthly_anniversary(): real dates, leap years, month-end, timezone boundaries -----

def test_anniversary_no_verified_epoch_fails_closed():
    ok, detail = check_monthly_anniversary(None, iso(2026, 10, 15))
    assert ok is False
    assert "no-verified-creation-timestamp" in detail


def test_anniversary_naive_timestamp_fails_closed():
    ok, detail = check_monthly_anniversary(epoch(2026, 9, 15), "2026-10-15T10:00:00")  # no tz
    assert ok is False
    assert "unparseable-or-naive" in detail


def test_anniversary_unparseable_timestamp_fails_closed():
    ok, detail = check_monthly_anniversary(epoch(2026, 9, 15), "not-a-real-date")
    assert ok is False


def test_anniversary_same_calendar_month_as_creation_is_not_eligible():
    created_epoch = epoch(2026, 9, 15)
    ok, detail = check_monthly_anniversary(created_epoch, iso(2026, 9, 20))
    assert ok is False
    assert "same-calendar-month" in detail


def test_anniversary_exact_one_month_later_is_eligible():
    created_epoch = epoch(2026, 9, 15)
    ok, detail = check_monthly_anniversary(created_epoch, iso(2026, 10, 15))
    assert ok is True


def test_anniversary_leap_year_feb29_creation_clamped_in_non_leap_february():
    """Created Feb 29, 2024 (leap year) -> the Feb 2025 (non-leap) anniversary must clamp to
    Feb 28, not fail to compute or wrap into March."""
    created_epoch = epoch(2024, 2, 29)
    ok, detail = check_monthly_anniversary(created_epoch, iso(2025, 2, 28))
    assert ok is True
    ok_march1, _ = check_monthly_anniversary(created_epoch, iso(2025, 3, 1), tolerance_days=0)
    assert ok_march1 is False  # one day past the clamped Feb 28 anniversary, zero tolerance


def test_anniversary_month_end_creation_clamps_to_shorter_month():
    """Created Jan 31 -> April (30 days) anniversary must clamp to April 30, not April 31
    (which doesn't exist) and not silently roll into May."""
    created_epoch = epoch(2026, 1, 31)
    ok, detail = check_monthly_anniversary(created_epoch, iso(2026, 4, 30))
    assert ok is True
    ok_may1, _ = check_monthly_anniversary(created_epoch, iso(2026, 5, 1), tolerance_days=0)
    assert ok_may1 is False


def test_anniversary_off_anniversary_usage_correction_is_rejected():
    """A usage drop that happens mid-month, nowhere near the verified anniversary day, must
    never be treated as eligible just because it looks like a reset."""
    created_epoch = epoch(2026, 9, 15)
    ok, detail = check_monthly_anniversary(created_epoch, iso(2026, 10, 2))  # 13 days early
    assert ok is False
    assert "off-anniversary" in detail


def test_anniversary_timezone_boundary_near_midnight_utc():
    """Same instant expressed in two different UTC offsets must agree -- the comparison is
    done in normalized UTC, not the literal local calendar date of each string."""
    created_epoch = epoch(2026, 9, 15, hour=23, minute=0)  # 23:00 +03:30 = 19:30 UTC on the 15th
    # One month later, same local time in a totally different offset (+00:00) landing at the
    # equivalent UTC instant should still be recognized as the same anniversary day in UTC.
    event_ts_utc_equivalent = datetime(2026, 10, 15, 19, 30, tzinfo=timezone.utc).isoformat()
    ok, detail = check_monthly_anniversary(created_epoch, event_ts_utc_equivalent)
    assert ok is True


def test_anniversary_tolerance_window_boundary():
    created_epoch = epoch(2026, 9, 15)
    within = check_monthly_anniversary(created_epoch, iso(2026, 10, 17), tolerance_days=2)[0]  # +2 days
    outside = check_monthly_anniversary(created_epoch, iso(2026, 10, 18), tolerance_days=2)[0]  # +3 days
    assert within is True
    assert outside is False


# 28. Receipt matching: trustworthy transaction identity, never fabricate evidence -------------

def test_receipt_match_requires_timing_not_amount_alone():
    """A receipt whose timestamp is clearly AFTER the ledger event it's being asked to
    explain must never be matched to it -- same vpsid+amount is not enough."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(30, 500, "T9"))  # suspiciously late timestamp
    eng.ingest_event(ev("QUOTA_CHANGE", 30, "300", "T1", "T2", old_q=1000, new_q=1500))
    assert len(eng.get_unmatched_quota_changes()) == 1  # NOT matched despite same vpsid+amount
    assert len(eng.get_ambiguous_quota_changes()) == 0


def test_receipt_match_rejects_inconsistent_before_after_quota():
    """A receipt that itself claims a before/after quota inconsistent with what the ledger
    actually observed must be rejected as a match, even with the same vpsid and amount --
    it is evidence of a DIFFERENT transaction, not confirmation of this one."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    r = receipt(31, 500, "T1.5")
    r.before_gb = Decimal("9000")   # does not match the ledger event's old_quota (1000) at all
    r.after_gb = Decimal("9500")
    eng.ingest_receipt(r)
    eng.ingest_event(ev("QUOTA_CHANGE", 31, "300", "T1", "T2", old_q=1000, new_q=1500))
    assert len(eng.get_unmatched_quota_changes()) == 1
    assert eng.receipts_by_vpsid[31][0]._consumed is False if hasattr(eng.receipts_by_vpsid[31][0], "_consumed") else True


def test_receipt_match_confirms_with_consistent_before_after_quota():
    eng = CarryoverEngine(fixed_reset_keys=set())
    r = receipt(32, 500, "T1.5")
    r.before_gb = Decimal("1000")
    r.after_gb = Decimal("1500")
    eng.ingest_receipt(r)
    eng.ingest_event(ev("QUOTA_CHANGE", 32, "300", "T1", "T2", old_q=1000, new_q=1500))
    assert len(eng.get_unmatched_quota_changes()) == 0
    assert len(eng.get_ambiguous_quota_changes()) == 0


def test_receipt_match_two_genuinely_distinct_candidates_is_ambiguous_not_guessed():
    """Two DIFFERENT real receipt files, same vpsid, same amount, both with plausible but
    unconfirmable timing (no before/after info) -- the engine must never guess which one
    funded this specific QUOTA_CHANGE event. Both must remain unconsumed and the event must
    be flagged ambiguous, never silently matched to whichever came first."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(33, 500, "T0.9", fname="receipt-A.json"))
    eng.ingest_receipt(receipt(33, 500, "T1.1", fname="receipt-B.json"))
    eng.ingest_event(ev("QUOTA_CHANGE", 33, "300", "T1", "T2", old_q=1000, new_q=1500))
    ambiguous = eng.get_ambiguous_quota_changes()
    assert len(eng.get_unmatched_quota_changes()) == 0
    assert len(ambiguous) == 1
    assert set(ambiguous[0]["candidate_receipt_files"]) == {"receipt-A.json", "receipt-B.json"}
    assert all(not getattr(r, "_consumed", False) for r in eng.receipts_by_vpsid[33])


def test_receipt_match_literal_duplicate_of_same_file_is_not_ambiguous():
    """The SAME receipt file re-ingested twice (identical source_file + timestamp) is one
    purchase seen twice, not two different purchases -- must still resolve to a clean match,
    not a false ambiguity. (Regression guard alongside test_duplicate_purchase_receipt_not_
    double_counted, which already exercises this path end-to-end.)"""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(34, 500, "T1.5", fname="same.json"))
    eng.ingest_receipt(receipt(34, 500, "T1.5", fname="same.json"))
    eng.ingest_event(ev("QUOTA_CHANGE", 34, "300", "T1", "T2", old_q=1000, new_q=1500))
    assert len(eng.get_ambiguous_quota_changes()) == 0
    assert len(eng.get_unmatched_quota_changes()) == 0


# 29. scan_verified_created_epochs(): fail-closed on conflicting evidence -----------------------

def test_scan_verified_created_epochs_single_source(tmp_path):
    (tmp_path / "r1.json").write_text(json.dumps({"items": [{"vpsid": 40, "created": 1790000000, "added_gb": 100}]}))
    verified, conflicts = scan_verified_created_epochs(str(tmp_path))
    assert verified.get(40) == 1790000000
    assert conflicts == {}


def test_scan_verified_created_epochs_conflicting_sources_excluded(tmp_path):
    (tmp_path / "r1.json").write_text(json.dumps({"before": {"1.2.3.4": {"vpsid": 41, "created": 1790000000}}}))
    (tmp_path / "r2.json").write_text(json.dumps({"before": {"1.2.3.4": {"vpsid": 41, "created": 1790000111}}}))
    verified, conflicts = scan_verified_created_epochs(str(tmp_path))
    assert 41 not in verified  # fail closed -- never guess which of two conflicting epochs is real
    assert conflicts[41] == [1790000000, 1790000111]


# 31. Exactly-once monthly grant per durable (vpsid, created_epoch, period) renewal identity ---

def test_duplicate_renewal_period_different_event_ids_grants_base_only_once():
    """Regression test for issue #1: two DIFFERENT events (different event_id, e.g. two
    slightly different monitoring snapshots of the same underlying reset -- here simulated by
    a tiny difference in new_used_gb) that both land in the SAME (vpsid, created_epoch,
    period_index) must only grant the 100 GB base ONCE. The second must be rejected as a
    duplicate renewal period, not silently granted again."""
    created_epoch = epoch(2026, 9, 15)
    eng = CarryoverEngine(fixed_reset_keys=set(), verified_created_epochs={70: created_epoch})
    e1 = ev("USAGE_DROP_REVIEW", 70, "created-70", iso(2026, 9, 15), iso(2026, 10, 15),
            old_q=1000, new_q=1000, old_u=900, new_u=1)
    # A different event_id (different new_used_gb -- e.g. a slightly later re-snapshot of the
    # same reset window) but the SAME renewal period.
    e2 = ev("USAGE_DROP_REVIEW", 70, "created-70", iso(2026, 9, 15), iso(2026, 10, 16),
            old_q=1000, new_q=1000, old_u=900, new_u=2)
    assert e1.event_id() != e2.event_id()  # genuinely different events, not a plain dedup case
    eng.ingest_event(e1)
    eng.ingest_event(e2)
    proposals = eng.get_proposals()
    assert len(proposals) == 2
    assert proposals[0].status == "READY_FOR_HUMAN_APPLICATION"
    assert proposals[0].renewal_period_key is not None
    assert proposals[1].status == "REJECTED_duplicate-renewal-period"
    assert proposals[1].kind == "DUPLICATE_RENEWAL_PERIOD"
    assert proposals[1].renewal_period_key is None
    assert proposals[0].renewal_period_key == f"70|{created_epoch}|1"


def test_different_renewal_periods_each_get_their_own_grant():
    """Sanity check that the duplicate-period guard does not over-reject: two events in
    genuinely DIFFERENT periods for the same vpsid must each be READY."""
    created_epoch = epoch(2026, 9, 15)
    eng = CarryoverEngine(fixed_reset_keys=set(), verified_created_epochs={71: created_epoch})
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 71, "created-71", iso(2026, 9, 15), iso(2026, 10, 15),
                         old_q=1000, new_q=1000, old_u=900, new_u=1))
    eng.ingest_event(ev("USAGE_DROP_REVIEW", 71, "created-71", iso(2026, 10, 15), iso(2026, 11, 15),
                         old_q=1050, new_q=1050, old_u=900, new_u=1))
    proposals = eng.get_proposals()
    assert len(proposals) == 2
    assert all(p.status == "READY_FOR_HUMAN_APPLICATION" for p in proposals)
    assert proposals[0].renewal_period_key != proposals[1].renewal_period_key


# 32. Receipt matching fails closed when ALL corroborating evidence is missing -----------------

def test_receipt_match_fails_closed_when_timing_and_quota_evidence_both_missing():
    """Regression test for issue #3: a receipt with NO timestamp and NO before/after quota
    info -- i.e. vpsid+amount is the ONLY thing it has in common with the ledger event -- must
    NEVER be auto-matched. Missing evidence is not confirmation."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(35, 500, ""))  # no timestamp at all, no before/after set
    eng.ingest_event(ev("QUOTA_CHANGE", 35, "300", "T1", "T2", old_q=1000, new_q=1500))
    assert len(eng.get_unmatched_quota_changes()) == 0  # a candidate DID exist...
    insufficient = eng.get_insufficient_evidence_quota_changes()
    assert len(insufficient) == 1  # ...but was correctly refused, not matched
    assert insufficient[0]["status"] == "INSUFFICIENT_EVIDENCE_NEEDS_HUMAN_REVIEW"
    assert all(not getattr(r, "_consumed", False) for r in eng.receipts_by_vpsid[35])


def test_receipt_match_timing_alone_is_sufficient_positive_evidence():
    """A receipt WITH a comparable timestamp (even with no before/after info) IS enough
    positive evidence to confirm -- only the all-missing case fails closed."""
    eng = CarryoverEngine(fixed_reset_keys=set())
    eng.ingest_receipt(receipt(36, 500, "T1.5"))  # has timing, no before/after
    eng.ingest_event(ev("QUOTA_CHANGE", 36, "300", "T1", "T2", old_q=1000, new_q=1500))
    assert len(eng.get_insufficient_evidence_quota_changes()) == 0
    assert len(eng.get_unmatched_quota_changes()) == 0


# 33. Billing-anchor date change mid-stream (policy edits the explicit anchor) -------------------

def test_billing_anchor_change_reanchors_future_eligibility_checks():
    """If an operator changes a VPS's billing anchor (e.g. corrects a wrong explicit date),
    the NEXT eligibility check must use the NEW anchor -- check_monthly_anniversary has no
    memory of a prior anchor, so this is really a test that changing the input changes the
    output deterministically and does not get stuck on a stale value anywhere."""
    old_anchor = epoch(2026, 9, 15)
    new_anchor = epoch(2026, 9, 1)
    event_ts = iso(2026, 10, 1)  # aligned to the NEW anchor's day-of-month, not the old one's
    ok_old, _ = check_monthly_anniversary(old_anchor, event_ts)
    ok_new, _ = check_monthly_anniversary(new_anchor, event_ts)
    assert ok_old is False  # Oct 1 is 14 days from the Sep-15 anniversary -- outside tolerance
    assert ok_new is True   # Oct 1 is exactly the Sep-1 anniversary


# 34. Clock drift: a small, plausible clock skew must not break a correctly-aligned renewal ------

def test_small_clock_drift_within_tolerance_still_eligible():
    created_epoch = epoch(2026, 9, 15)
    # Simulates a monitoring host whose clock is ~90 minutes fast/slow -- well within the
    # ±2-day tolerance window, so it must not affect the eligibility outcome at all.
    drifted_event_ts = datetime(2026, 10, 15, 11, 30, tzinfo=TZ).isoformat()
    ok, _ = check_monthly_anniversary(created_epoch, drifted_event_ts)
    assert ok is True


def test_large_clock_drift_beyond_tolerance_is_rejected_not_silently_trusted():
    """A gross clock error (here, several days) must still be rejected by the same tolerance
    rule as any other off-anniversary timestamp -- there is no special-case leniency for
    'probably just clock drift.' This is intentional: the system cannot distinguish genuine
    drift from a wrong/attacker-influenced clock, so it must fail closed either way."""
    created_epoch = epoch(2026, 9, 15)
    drifted_event_ts = iso(2026, 10, 20)  # 5 days off -- outside the ±2-day tolerance
    ok, detail = check_monthly_anniversary(created_epoch, drifted_event_ts)
    assert ok is False
    assert "off-anniversary" in detail
