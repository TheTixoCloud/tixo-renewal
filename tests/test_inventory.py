import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import inventory as inv


def make_parse_result(records, complete=True, error=None):
    return {"complete": complete, "records": records, "skipped_count": 0, "error": error}


def rec(vpsid, hostname="h", status="running", quota="100", used="10", ram="1024",
        cores="1", disk="20", virt="kvm", locked="unavailable", node_serid="unavailable"):
    return {"vpsid": vpsid, "hostname": hostname, "status": status, "quota_gb": Decimal(quota),
            "used_gb": Decimal(used), "ram_mb": ram, "cores": cores, "disk_gb": disk,
            "virtualization": virt, "locked": locked, "node_serid": node_serid}


def test_ingest_rejects_incomplete_snapshot_without_writing(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    result = inv.ingest_snapshot(conn, "self", make_parse_result([], complete=False, error="bad"))
    assert result["status"] == "REJECTED_INCOMPLETE"
    assert inv.latest_snapshot_id(conn, "self") is None


def test_first_snapshot_reports_all_as_added_never_as_removed(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    result = inv.ingest_snapshot(conn, "self", make_parse_result([rec(1), rec(2)]))
    assert result["status"] == "OK"
    assert sorted(result["diff"]["added"]) == [1, 2]
    assert result["diff"]["removed"] == []


def test_second_snapshot_detects_added_and_removed(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1), rec(2)]))
    result = inv.ingest_snapshot(conn, "self", make_parse_result([rec(2), rec(3)]))
    assert result["diff"]["added"] == [3]
    assert result["diff"]["removed"] == [1]


def test_modified_quota_detected_and_flagged_as_quota_changed(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, quota="100")]))
    result = inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, quota="250")]))
    assert result["diff"]["quota_changed"] == [1]
    mod = result["diff"]["modified"][0]
    assert mod["changes"]["quota_gb"]["old"] == "100"
    assert mod["changes"]["quota_gb"]["new"] == "250"


def test_unchanged_vps_produces_no_modified_entry(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1)]))
    result = inv.ingest_snapshot(conn, "self", make_parse_result([rec(1)]))
    assert result["diff"]["modified"] == []
    assert result["diff"]["added"] == []
    assert result["diff"]["removed"] == []


def test_incomplete_snapshot_never_counted_as_the_diff_baseline(tmp_path):
    """The core requirement: a partial/failed sync must never cause the NEXT real sync to
    think VPSs were deleted."""
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1), rec(2)]))
    rejected = inv.ingest_snapshot(conn, "self", make_parse_result([], complete=False, error="timeout"))
    assert rejected["status"] == "REJECTED_INCOMPLETE"
    result = inv.ingest_snapshot(conn, "self", make_parse_result([rec(1), rec(2)]))
    assert result["diff"]["removed"] == []
    assert result["diff"]["added"] == []


def test_separate_node_ids_do_not_cross_contaminate_diffs(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "node-a", make_parse_result([rec(1)]))
    result = inv.ingest_snapshot(conn, "node-b", make_parse_result([rec(99)]))
    assert result["diff"]["added"] == [99]
    assert result["diff"]["removed"] == []


def test_detect_id_reuse_flags_different_quota_after_reappearance(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, quota="100")]))
    inv.ingest_snapshot(conn, "self", make_parse_result([]))  # vpsid 1 vanishes... wait, incomplete
    # (empty complete=True snapshot is a legitimate "fleet now empty" state for this test)
    assert inv.detect_id_reuse(conn, "self", 1, "9999") is True


def test_detect_id_reuse_false_for_unknown_vpsid(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    assert inv.detect_id_reuse(conn, "self", 12345, "50") is False


def test_latest_records_reflects_most_recent_snapshot_only(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, quota="100")]))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, quota="200")]))
    records = inv.latest_records(conn, "self")
    assert len(records) == 1
    assert records[0]["quota_gb"] == "200"


def test_last_sync_info_none_when_never_synced(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    assert inv.last_sync_info(conn, "self") is None


def test_last_sync_info_reports_captured_at_and_count(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1), rec(2)]), captured_at="2026-01-01T00:00:00+00:00")
    info = inv.last_sync_info(conn, "self")
    assert info["captured_at"] == "2026-01-01T00:00:00+00:00"
    assert info["record_count"] == 2


# ---------- v1.5.0 regression: used_gb fluctuation must never cause a false "modified" ----

def test_used_gb_changing_alone_never_counts_as_modified_regression(tmp_path):
    """Regression test for the actual production bug: 42 of ~60 real VPSs were reported as
    'modified' on every sync with zero real configuration change behind it, because routine
    bandwidth usage (used_gb) ticking up between polls was included in the config-change
    diff. Simulates the same shape at real scale: 60 VPSs, all of them accumulate some
    bandwidth usage between two syncs, none of them have any other field change."""
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    first_batch = [rec(i, quota="100", used="10") for i in range(1, 61)]
    inv.ingest_snapshot(conn, "self", make_parse_result(first_batch))
    second_batch = [rec(i, quota="100", used=str(10 + i)) for i in range(1, 61)]  # only usage ticked up
    result = inv.ingest_snapshot(conn, "self", make_parse_result(second_batch))
    assert result["diff"]["modified_count"] == 0
    assert result["diff"]["modified"] == []
    assert len(result["diff"]["usage_trend"]) == 60


def test_usage_trend_captures_old_and_new_used_gb():
    conn_path_records = [rec(1, used="10")]
    # exercised indirectly via ingest_snapshot in the test above; this test checks the pure
    # diff function directly for the exact old/new values reported.
    prev = {1: {"hostname": "h", "status": "running", "quota_gb": "100", "used_gb": "10",
                "ram_mb": "1024", "cores": "1", "disk_gb": "20", "virtualization": "kvm",
                "locked": "unavailable", "node_serid": "unavailable"}}
    current = {1: {"vpsid": 1, "hostname": "h", "status": "running", "quota_gb": Decimal("100"),
                   "used_gb": Decimal("25"), "ram_mb": "1024", "cores": "1", "disk_gb": "20",
                   "virtualization": "kvm", "locked": "unavailable", "node_serid": "unavailable"}}
    diff = inv._diff_snapshots(prev, current)
    assert diff["modified"] == []
    assert diff["usage_trend"] == [{"vpsid": 1, "old_used_gb": "10", "new_used_gb": "25"}]


def test_real_configuration_change_still_detected_as_modified_alongside_usage_drift(tmp_path):
    """Config changes must still be caught even while used_gb also drifts in the same sync --
    the fix excludes used_gb specifically, not the whole diff mechanism."""
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, quota="100", used="10", ram="1024")]))
    result = inv.ingest_snapshot(conn, "self",
                                  make_parse_result([rec(1, quota="100", used="50", ram="2048")]))
    assert result["diff"]["modified_count"] == 1
    assert result["diff"]["modified"][0]["changes"]["ram_mb"] == {"old": "1024", "new": "2048"}
    assert "used_gb" not in result["diff"]["modified"][0]["changes"]
    assert result["diff"]["usage_trend"] == [{"vpsid": 1, "old_used_gb": "10", "new_used_gb": "50"}]


def test_last_sync_info_includes_authoritative_total(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    parse_result = make_parse_result([rec(1), rec(2)])
    parse_result["authoritative_total"] = 2
    inv.ingest_snapshot(conn, "self", parse_result)
    info = inv.last_sync_info(conn, "self")
    assert info["authoritative_total"] == 2


def test_last_sync_info_authoritative_total_none_when_unknown(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1)]))
    info = inv.last_sync_info(conn, "self")
    assert info["authoritative_total"] is None
