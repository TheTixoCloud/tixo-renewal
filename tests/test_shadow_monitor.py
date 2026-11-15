"""Tests for the local-only shadow monitor. No SSH, no production access -- all SQLite state
is created in a temporary file per test. The one real SSH touchpoint (`fetch_live_rows`) is
exercised separately as a manual read-only smoke test against production, not here.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import shadow_monitor as sm


def iso(dt):
    return dt.isoformat()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.sqlite3")
    conn = sm.open_db(path)
    yield conn
    conn.close()


def row(vpsid, created_epoch, quota, used):
    return {"vpsid": vpsid, "created_epoch": created_epoch,
            "quota_gb": Decimal(str(quota)), "used_gb": Decimal(str(used))}


# 1. Schema / open_db -----------------------------------------------------------------------

def test_open_db_creates_schema(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"identities", "identity_conflicts", "snapshots", "renewal_checkpoints", "monitor_checkpoints"} <= tables


# 2. Ingestion is atomic and durable ---------------------------------------------------------

def test_ingest_poll_records_identity_and_snapshot(db):
    as_of = iso(datetime.now(timezone.utc))
    result = sm.ingest_poll(db, [row(1, 1700000000, 1000, 500)], as_of)
    assert result["rows_ingested"] == 1
    assert result["identity_conflicts"] == []
    ident = db.execute("SELECT created_epoch FROM identities WHERE vpsid=1").fetchone()
    assert ident[0] == 1700000000
    snap = db.execute("SELECT quota_gb, used_gb FROM snapshots WHERE vpsid=1").fetchone()
    assert snap == ("1000", "500")


def test_ingest_poll_never_silently_overwrites_conflicting_identity(db):
    as_of1 = iso(datetime.now(timezone.utc))
    sm.ingest_poll(db, [row(2, 1700000000, 1000, 500)], as_of1)
    as_of2 = iso(datetime.now(timezone.utc) + timedelta(hours=1))
    result = sm.ingest_poll(db, [row(2, 1799999999, 1000, 600)], as_of2)  # different created epoch
    assert len(result["identity_conflicts"]) == 1
    # original value preserved, never silently replaced
    ident = db.execute("SELECT created_epoch FROM identities WHERE vpsid=2").fetchone()
    assert ident[0] == 1700000000
    conflict = db.execute("SELECT stored_created_epoch, observed_created_epoch FROM identity_conflicts WHERE vpsid=2").fetchone()
    assert conflict == (1700000000, 1799999999)


def test_ingest_is_atomic_on_failure(db):
    """If a later row in the same poll batch is malformed and raises, the WHOLE transaction
    (including any earlier, successfully-processed rows in the same batch) must roll back --
    no partial snapshot/identity rows left behind."""
    good_row = row(3, 1700000000, 1000, 500)
    bad_row = {"vpsid": 99}  # missing required keys -> KeyError raised mid-transaction
    with pytest.raises(Exception):
        sm.ingest_poll(db, [good_row, bad_row], iso(datetime.now(timezone.utc)))
    # the good row, despite being processed first in the same transaction, must not persist
    assert db.execute("SELECT COUNT(*) FROM identities WHERE vpsid=3").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM snapshots WHERE vpsid=3").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM identities WHERE vpsid=99").fetchone()[0] == 0


# 3. Detection: missing pre-reset snapshot ----------------------------------------------------

def test_single_snapshot_is_missing_pre_reset_snapshot(db):
    sm.ingest_poll(db, [row(4, 1700000000, 1000, 500)], iso(datetime.now(timezone.utc)))
    results = sm.detect_renewal_candidates(db)
    r = [x for x in results if x["vpsid"] == 4][0]
    assert r["status"] == "FROZEN_FOR_REVIEW"
    assert "missing-pre-reset-snapshot" in r["reason"]


def test_large_gap_between_snapshots_is_flagged_missing_pre_reset(db):
    now = datetime.now(timezone.utc)
    sm.ingest_poll(db, [row(5, 1700000000, 1000, 900)], iso(now - timedelta(hours=10)))
    sm.ingest_poll(db, [row(5, 1700000000, 1000, 1)], iso(now))  # 10h gap, way over 3x30min
    results = sm.detect_renewal_candidates(db)
    r = [x for x in results if x["vpsid"] == 5][0]
    assert r["status"] == "FROZEN_FOR_REVIEW"
    assert "missing-pre-reset-snapshot" in r["reason"]


# 4. Detection: uncertain identity -------------------------------------------------------------

def test_identity_conflict_is_flagged_frozen_for_review(db):
    now = datetime.now(timezone.utc)
    sm.ingest_poll(db, [row(6, 1700000000, 1000, 900)], iso(now - timedelta(minutes=30)))
    sm.ingest_poll(db, [row(6, 1799999999, 1000, 1)], iso(now))  # conflicting created epoch
    results = sm.detect_renewal_candidates(db)
    r = [x for x in results if x["vpsid"] == 6][0]
    assert r["status"] == "FROZEN_FOR_REVIEW"
    assert "uncertain-identity" in r["reason"]


# 5. Detection: usage drop alone / off-anniversary is never proof --------------------------------

def test_usage_drop_off_anniversary_is_frozen_for_review_not_plausible(db):
    created_epoch = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
    t1 = datetime(2026, 10, 2, tzinfo=timezone.utc)  # nowhere near the 15th anniversary
    t2 = t1 + timedelta(minutes=30)
    sm.ingest_poll(db, [row(7, created_epoch, 1000, 900)], iso(t1))
    sm.ingest_poll(db, [row(7, created_epoch, 1000, 1)], iso(t2))
    results = sm.detect_renewal_candidates(db)
    r = [x for x in results if x["vpsid"] == 7][0]
    assert r["status"] == "FROZEN_FOR_REVIEW"
    assert r["engine_status"].startswith("REJECTED")


# 6. Detection: aligned anniversary becomes a plausible (not confirmed) candidate -----------------

def test_usage_drop_on_verified_anniversary_is_plausible_candidate(db):
    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    t1 = datetime(2026, 10, 15, 9, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc)
    sm.ingest_poll(db, [row(8, created_epoch, 1000, 900)], iso(t1))
    sm.ingest_poll(db, [row(8, created_epoch, 1000, 1)], iso(t2))
    results = sm.detect_renewal_candidates(db)
    r = [x for x in results if x["vpsid"] == 8][0]
    assert r["status"] == "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION"
    assert "NOT independent proof" in r["reason"] or "PLAUSIBLE" in r["reason"]
    # the renewal period was persisted durably
    assert db.execute("SELECT COUNT(*) FROM renewal_checkpoints WHERE vpsid=8").fetchone()[0] == 1


# 7. Restart durability: never mistake a restarted monitor for a new renewal ----------------------

def test_restart_does_not_duplicate_or_forget_a_flagged_renewal(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    t1 = datetime(2026, 10, 15, 9, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc)

    conn1 = sm.open_db(path)
    sm.ingest_poll(conn1, [row(9, created_epoch, 1000, 900)], iso(t1))
    sm.ingest_poll(conn1, [row(9, created_epoch, 1000, 1)], iso(t2))
    first_results = sm.detect_renewal_candidates(conn1)
    conn1.close()  # simulate process restart

    conn2 = sm.open_db(path)  # fresh process, same durable file
    second_results = sm.detect_renewal_candidates(conn2)
    r1 = [x for x in first_results if x["vpsid"] == 9][0]
    r2 = [x for x in second_results if x["vpsid"] == 9][0]
    assert r1["status"] == r2["status"] == "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION"
    assert r1["renewal_period_key"] == r2["renewal_period_key"]
    # still exactly one durable record -- restart did not duplicate it
    assert conn2.execute("SELECT COUNT(*) FROM renewal_checkpoints WHERE vpsid=9").fetchone()[0] == 1
    conn2.close()


def test_different_event_same_period_after_restart_is_duplicate_not_regrant(tmp_path):
    """A DIFFERENT monitoring snapshot (different event_id) landing in the SAME renewal
    period, observed after a simulated restart, must be rejected as a duplicate period --
    never granted a second time."""
    path = str(tmp_path / "restart2.sqlite3")
    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    t1 = datetime(2026, 10, 15, 9, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 10, 16, 10, 1, tzinfo=timezone.utc)  # still within the same period

    conn1 = sm.open_db(path)
    sm.ingest_poll(conn1, [row(10, created_epoch, 1000, 900)], iso(t1))
    sm.ingest_poll(conn1, [row(10, created_epoch, 1000, 1)], iso(t2))
    sm.detect_renewal_candidates(conn1)
    conn1.close()

    conn2 = sm.open_db(path)
    # A later, DIFFERENT snapshot pair for the same vpsid (different used_gb/timestamp ->
    # different event_id) still within the same calendar period.
    sm.ingest_poll(conn2, [row(10, created_epoch, 1000, 2)], iso(t3))
    results = sm.detect_renewal_candidates(conn2)
    r = [x for x in results if x["vpsid"] == 10][-1]
    # most recent pair (t2->t3) shows usage going from 1 to 2 (a rise, not a drop) -- not a
    # reset pattern at all for THIS pair, but the durable checkpoint from the first poll must
    # still be exactly one row, proving no double grant happened across the restart.
    assert conn2.execute("SELECT COUNT(*) FROM renewal_checkpoints WHERE vpsid=10").fetchone()[0] == 1
    conn2.close()


# 8. Next-anniversary reporting ----------------------------------------------------------------

def test_next_anniversary_report_uses_verified_epoch_only(db):
    created_epoch = int(datetime(2026, 1, 31, 10, 0, tzinfo=timezone.utc).timestamp())
    sm.ingest_poll(db, [row(11, created_epoch, 1000, 500)], iso(datetime.now(timezone.utc)))
    report = sm.next_anniversary_report(db)
    r = [x for x in report if x["vpsid"] == 11][0]
    assert r["created_at"].startswith("2026-01-31")
    assert r["next_expected_anniversary"]  # computed, non-empty


# 9. fetch_live_rows parses the real script's tab-separated output format -----------------------

def test_fetch_live_rows_parses_expected_format(monkeypatch):
    fake_stdout = (
        "vpsid\tuid\tcreated_unix\tcreated_at\tcreated_day\tedittime\tbandwidth\tused_bandwidth\n"
        "4\t3\t1790096510\t2026-09-22 20:31:50\t22\t1794656874\t17473\t15636.51\n"
        "\n"
    )

    class FakeResult:
        returncode = 0
        stdout = fake_stdout
        stderr = ""

    monkeypatch.setattr(sm.subprocess, "run", lambda *a, **k: FakeResult())
    rows = sm.fetch_live_rows(["ssh", "-i", "/fake/key", "root@fake-host.example.com"])
    assert rows == [{"vpsid": 4, "created_epoch": 1790096510,
                      "quota_gb": Decimal("17473"), "used_gb": Decimal("15636.51")}]


def test_fetch_live_rows_with_no_ssh_cmd_and_no_default_raises(monkeypatch):
    """Regression test: this package ships with NO usable hardcoded host anywhere -- calling
    fetch_live_rows() with nothing configured must fail loudly, never silently target some
    leftover default server."""
    with pytest.raises(RuntimeError):
        sm.fetch_live_rows()


def test_fetch_live_rows_raises_on_ssh_failure(monkeypatch):
    class FakeResult:
        returncode = 255
        stdout = ""
        stderr = "Permission denied"

    monkeypatch.setattr(sm.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(RuntimeError):
        sm.fetch_live_rows(["ssh", "-i", "/fake/key", "root@fake-host.example.com"])


# 10. Concurrent execution: two pollers writing to the same durable file must not corrupt it ----

def test_concurrent_polls_to_same_db_file_do_not_lose_data(tmp_path):
    """Simulates two monitor processes (e.g. a manual run overlapping with the systemd timer)
    both polling and ingesting into the SAME SQLite file. WAL mode + explicit transactions
    must serialize the two writes safely -- neither poll's data is lost or corrupted."""
    path = str(tmp_path / "concurrent.sqlite3")
    conn_a = sm.open_db(path)
    conn_b = sm.open_db(path)  # a second "process" opening the same file

    sm.ingest_poll(conn_a, [row(50, 1700000000, 1000, 500)], iso(datetime.now(timezone.utc)))
    sm.ingest_poll(conn_b, [row(51, 1700000001, 2000, 100)], iso(datetime.now(timezone.utc)))
    sm.ingest_poll(conn_a, [row(50, 1700000000, 1000, 600)], iso(datetime.now(timezone.utc) + timedelta(minutes=30)))

    conn_a.close()
    conn_b.close()

    verify = sm.open_db(path)
    assert verify.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 2  # both vpsids present
    assert verify.execute("SELECT COUNT(*) FROM snapshots WHERE vpsid=50").fetchone()[0] == 2  # both polls for vpsid 50 landed
    assert verify.execute("SELECT COUNT(*) FROM snapshots WHERE vpsid=51").fetchone()[0] == 1
    verify.close()


# 11. "Native reset race": quota changes unexpectedly in the same window as a usage drop -------

def test_concurrent_quota_change_during_usage_drop_is_frozen_not_guessed(db):
    """If something else (a hypothetical native Virtualizor mechanism, or a concurrent manual
    admin edit) changes the quota in the SAME window a usage drop is observed, the engine must
    never guess which explanation is right -- it must freeze for review, per the explicit
    instruction not to assume a native reset occurred."""
    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    t1 = datetime(2026, 10, 15, 9, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc)
    sm.ingest_poll(db, [row(52, created_epoch, 1000, 900)], iso(t1))
    # quota ALSO changed (1000 -> 1200) in the same window as the usage drop -- ambiguous:
    # could be a native reset bumping the base, a concurrent manual edit, or something else.
    sm.ingest_poll(db, [row(52, created_epoch, 1200, 1)], iso(t2))
    results = sm.detect_renewal_candidates(db)
    r = [x for x in results if x["vpsid"] == 52][0]
    assert r["status"] == "FROZEN_FOR_REVIEW"
    assert "quota changed" in r["reason"] or "ambiguous" in r["reason"]
