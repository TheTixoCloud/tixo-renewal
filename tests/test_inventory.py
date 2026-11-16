import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import inventory as inv


def make_parse_result(records, complete=True, error=None):
    return {"complete": complete, "records": records, "skipped_count": 0, "error": error}


def rec(vpsid, hostname="h", status="running", quota="100", used="10", ram="1024",
        cores="1", disk="20", virt="kvm", locked="unavailable", node_serid="unavailable",
        ip_primary="unavailable", ip_count="unavailable"):
    return {"vpsid": vpsid, "hostname": hostname, "status": status, "quota_gb": Decimal(quota),
            "used_gb": Decimal(used), "ram_mb": ram, "cores": cores, "disk_gb": disk,
            "virtualization": virt, "locked": locked, "node_serid": node_serid,
            "ip_primary": ip_primary, "ip_count": ip_count}


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


# ---------- v1.6.0: idempotent, version-aware schema migrations (fixes the real v1.5.0 bug) --

def test_open_db_on_fresh_database_has_all_columns(tmp_path):
    conn = inv.open_db(str(tmp_path / "fresh.sqlite3"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)").fetchall()]
    assert "authoritative_total" in cols
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(snapshot_records)").fetchall()]
    assert "ip_primary" in cols2
    assert "ip_count" in cols2


def test_migration_adds_missing_columns_to_pre_existing_older_schema_database(tmp_path):
    """Reproduces the actual v1.5.0 production bug: a database created by OLDER code (no
    authoritative_total/ip_primary/ip_count columns) must gain them on next open_db(),
    without losing any existing data."""
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            record_count INTEGER NOT NULL
        );
        CREATE TABLE snapshot_records (
            snapshot_id INTEGER NOT NULL,
            vpsid INTEGER NOT NULL,
            hostname TEXT, status TEXT, quota_gb TEXT, used_gb TEXT,
            ram_mb TEXT, cores TEXT, disk_gb TEXT, virtualization TEXT,
            locked TEXT, node_serid TEXT,
            PRIMARY KEY (snapshot_id, vpsid)
        );
        CREATE TABLE known_vps (
            node_id TEXT NOT NULL, vpsid INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_seen_quota_gb TEXT,
            PRIMARY KEY (node_id, vpsid)
        );
    """)
    conn.execute("INSERT INTO snapshots (node_id, captured_at, record_count) VALUES ('self', 'x', 5)")
    conn.execute("INSERT INTO snapshot_records (snapshot_id, vpsid, hostname) VALUES (1, 1, 'preserved-host')")
    conn.commit()
    conn.close()

    migrated = inv.open_db(path)
    cols = [r[1] for r in migrated.execute("PRAGMA table_info(snapshots)").fetchall()]
    assert "authoritative_total" in cols
    cols2 = [r[1] for r in migrated.execute("PRAGMA table_info(snapshot_records)").fetchall()]
    assert "ip_primary" in cols2 and "ip_count" in cols2
    # existing data preserved
    row = migrated.execute("SELECT record_count FROM snapshots WHERE id=1").fetchone()
    assert row[0] == 5
    hostname = migrated.execute("SELECT hostname FROM snapshot_records WHERE vpsid=1").fetchone()
    assert hostname[0] == "preserved-host"


def test_repeated_migration_execution_is_safe_noop(tmp_path):
    path = str(tmp_path / "repeat.sqlite3")
    conn1 = inv.open_db(path)
    conn1.close()
    conn2 = inv.open_db(path)  # second open -- migrations must not error on an already-migrated db
    conn3 = inv.open_db(path)  # third time, for good measure
    cols = [r[1] for r in conn3.execute("PRAGMA table_info(snapshots)").fetchall()]
    assert "authoritative_total" in cols


def test_ensure_schema_migrations_directly_idempotent(tmp_path):
    conn = inv.open_db(str(tmp_path / "direct.sqlite3"))
    inv.ensure_schema_migrations(conn)
    inv.ensure_schema_migrations(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)").fetchall()]
    assert cols.count("authoritative_total") == 1


def test_get_usage_trend_for_vps_returns_oldest_first(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, used="10")]),
                        captured_at="2026-01-01T00:00:00+00:00")
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, used="20")]),
                        captured_at="2026-01-02T00:00:00+00:00")
    inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, used="30")]),
                        captured_at="2026-01-03T00:00:00+00:00")
    trend = inv.get_usage_trend_for_vps(conn, "self", 1)
    assert [t["used_gb"] for t in trend] == ["10", "20", "30"]
    assert trend[0]["captured_at"] == "2026-01-01T00:00:00+00:00"


def test_get_usage_trend_for_vps_respects_limit(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    for i in range(5):
        inv.ingest_snapshot(conn, "self", make_parse_result([rec(1, used=str(i))]),
                            captured_at=f"2026-01-0{i+1}T00:00:00+00:00")
    trend = inv.get_usage_trend_for_vps(conn, "self", 1, limit=2)
    assert len(trend) == 2
    assert trend[-1]["used_gb"] == "4"


def test_get_usage_trend_for_vps_empty_when_never_seen(tmp_path):
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    assert inv.get_usage_trend_for_vps(conn, "self", 999) == []
