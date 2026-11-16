import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import inventory as inv
import inventory_sync as isync


def test_lock_prevents_concurrent_acquisition(tmp_path):
    state_dir = str(tmp_path)
    fh1 = isync.acquire_inventory_lock(state_dir)
    try:
        import pytest
        with pytest.raises(isync.InventoryLockHeld):
            isync.acquire_inventory_lock(state_dir)
    finally:
        isync.release_inventory_lock(fh1)


def test_lock_releasable_and_reacquirable(tmp_path):
    state_dir = str(tmp_path)
    fh1 = isync.acquire_inventory_lock(state_dir)
    isync.release_inventory_lock(fh1)
    fh2 = isync.acquire_inventory_lock(state_dir)
    isync.release_inventory_lock(fh2)


def test_sync_state_roundtrip(tmp_path):
    state_dir = str(tmp_path)
    assert isync.load_sync_state(state_dir)["last_attempt_status"] is None
    isync.save_sync_state(state_dir, "OK")
    assert isync.load_sync_state(state_dir)["last_attempt_status"] == "OK"


def test_classify_freshness_lock_held_takes_priority():
    assert isync.classify_freshness(None, None, 10, lock_held=True) == isync.REFRESHING


def test_classify_freshness_never_synced_is_stale():
    assert isync.classify_freshness(None, None, 10) == isync.STALE


def test_classify_freshness_recent_sync_is_fresh():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=2)).isoformat()
    assert isync.classify_freshness(recent, "OK", 10, now=now) == isync.FRESH


def test_classify_freshness_old_sync_is_stale():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=5)).isoformat()
    assert isync.classify_freshness(old, "OK", 10, now=now) == isync.STALE


def test_classify_freshness_rejected_incomplete_is_partial():
    assert isync.classify_freshness("2026-01-01T00:00:00+00:00", "REJECTED_INCOMPLETE", 10) == isync.PARTIAL


def test_classify_freshness_error_status_is_error():
    assert isync.classify_freshness("2026-01-01T00:00:00+00:00", "ERROR", 10) == isync.ERROR


def test_bounded_retry_succeeds_after_transient_failure():
    attempts = {"count": 0}
    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise ConnectionError("transient")
        return "ok"
    result = isync._call_with_bounded_retry(flaky, max_retries=2, sleep_fn=lambda s: None)
    assert result == "ok"
    assert attempts["count"] == 2


def test_bounded_retry_gives_up_after_max_retries():
    import pytest
    def always_fails():
        raise ConnectionError("still down")
    with pytest.raises(ConnectionError):
        isync._call_with_bounded_retry(always_fails, max_retries=2, sleep_fn=lambda s: None)


def test_run_inventory_sync_happy_path(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    def fake_call_listvs(creds):
        return {"raw": "...", "parsed": {"vs": {"1.2.3.4": {"vpsid": "1", "bandwidth": "100",
                                                              "used_bandwidth": "10"}}}}
    result = isync.run_inventory_sync(conn, "self", creds=None, state_dir=str(tmp_path),
                                       call_listvs_api=fake_call_listvs)
    assert result["status"] == "OK"
    assert result["diff"]["added"] == [1]
    state = isync.load_sync_state(str(tmp_path))
    assert state["last_attempt_status"] == "OK"


def test_run_inventory_sync_reports_error_without_raising(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    def fake_call_listvs(creds):
        raise ConnectionError("network down")
    result = isync.run_inventory_sync(conn, "self", creds=None, state_dir=str(tmp_path),
                                       call_listvs_api=fake_call_listvs, max_retries=0,
                                       sleep_fn=lambda s: None)
    assert result["status"] == "ERROR"
    assert "network down" in result["error"]


def test_run_inventory_sync_reports_partial_on_incomplete_parse(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    def fake_call_listvs(creds):
        return {"raw": "...", "parsed": {"vs": {"1.2.3.4": {"hostname": "no-vpsid"}}}}
    result = isync.run_inventory_sync(conn, "self", creds=None, state_dir=str(tmp_path),
                                       call_listvs_api=fake_call_listvs)
    assert result["status"] == "PARTIAL"


def test_run_inventory_sync_refuses_when_lock_already_held(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    fh = isync.acquire_inventory_lock(str(tmp_path))
    try:
        result = isync.run_inventory_sync(conn, "self", creds=None, state_dir=str(tmp_path),
                                           call_listvs_api=lambda creds: {"parsed": {"vs": {}}})
        assert result["status"] == "ALREADY_RUNNING"
    finally:
        isync.release_inventory_lock(fh)


def test_run_inventory_sync_never_touches_write_approval_marker(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    marker = tmp_path / "WRITE_APPROVED"
    def fake_call_listvs(creds):
        return {"parsed": {"vs": {"1.2.3.4": {"vpsid": "1", "bandwidth": "100"}}}}
    isync.run_inventory_sync(conn, "self", creds=None, state_dir=str(tmp_path),
                              call_listvs_api=fake_call_listvs)
    assert not marker.exists()
