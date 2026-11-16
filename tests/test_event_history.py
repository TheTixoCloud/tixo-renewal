import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import event_history as eh
import writer as w


def no_prior(vpsid):
    return None


def test_vps_created_confirmed_when_never_seen_before():
    diff = {"added": [1], "removed": [], "modified": [], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert events == [{"vpsid": 1, "event_type": eh.EVENT_VPS_CREATED, "confidence": eh.CONFIRMED,
                       "old_value": None, "new_value": None,
                       "evidence": "first appearance of this vpsid in any complete snapshot"}]


def test_vps_id_reuse_suspected_when_known_vps_reappears():
    diff = {"added": [5], "removed": [], "modified": [], "usage_trend": []}
    def prior(vpsid):
        return {"first_seen_at": "2026-01-01T00:00:00+00:00"} if vpsid == 5 else None
    events = eh.derive_events(diff, prior, "self")
    assert events[0]["event_type"] == eh.EVENT_VPS_ID_REUSE_SUSPECTED
    assert events[0]["confidence"] == eh.POSSIBLE


def test_vps_deleted_confirmed():
    diff = {"added": [], "removed": [9], "modified": [], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_VPS_DELETED
    assert events[0]["confidence"] == eh.CONFIRMED


def test_node_serid_change_is_migration():
    diff = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"node_serid": {"old": "1", "new": "2"}}}], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_VPS_MIGRATION
    assert events[0]["confidence"] == eh.CONFIRMED


def test_ip_primary_change_is_ip_reassignment():
    diff = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"ip_primary": {"old": "1.2.3.4", "new": "1.2.3.5"}}}], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_IP_REASSIGNMENT


def test_hostname_change():
    diff = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"hostname": {"old": "a", "new": "b"}}}], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_HOSTNAME_CHANGE


def test_resource_changes_ram_cores_disk():
    diff = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"ram_mb": {"old": "1024", "new": "2048"},
                                  "cores": {"old": "1", "new": "2"},
                                  "disk_gb": {"old": "20", "new": "40"}}}], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert len(events) == 3
    assert all(e["event_type"] == eh.EVENT_RESOURCE_CHANGE for e in events)


def test_lock_enabled_and_disabled():
    diff_lock = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"locked": {"old": "False", "new": "True"}}}], "usage_trend": []}
    events = eh.derive_events(diff_lock, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_LOCK_ENABLED

    diff_unlock = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"locked": {"old": "True", "new": "False"}}}], "usage_trend": []}
    events = eh.derive_events(diff_unlock, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_LOCK_DISABLED


def test_quota_increase_and_decrease():
    diff_inc = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"quota_gb": {"old": "100", "new": "200"}}}], "usage_trend": []}
    events = eh.derive_events(diff_inc, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_QUOTA_INCREASE
    assert "NOT evidence of a renewal-eligible reset" in events[0]["evidence"]

    diff_dec = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"quota_gb": {"old": "200", "new": "100"}}}], "usage_trend": []}
    events = eh.derive_events(diff_dec, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_QUOTA_DECREASE


def test_limited_to_unlimited_and_back():
    diff = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"quota_gb": {"old": "100", "new": "0"}}}], "usage_trend": []}
    events = eh.derive_events(diff, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_LIMITED_TO_UNLIMITED

    diff2 = {"added": [], "removed": [], "modified": [
        {"vpsid": 1, "changes": {"quota_gb": {"old": "0", "new": "100"}}}], "usage_trend": []}
    events2 = eh.derive_events(diff2, no_prior, "self")
    assert events2[0]["event_type"] == eh.EVENT_UNLIMITED_TO_LIMITED


def test_used_increase_observed():
    diff = {"added": [], "removed": [], "modified": [], "usage_trend": [
        {"vpsid": 1, "old_used_gb": "10", "new_used_gb": "20"}]}
    events = eh.derive_events(diff, no_prior, "self")
    assert events[0]["event_type"] == eh.EVENT_USED_INCREASE_OBSERVED
    assert events[0]["confidence"] == eh.CONFIRMED


def test_used_decrease_without_ledger_is_possible_reset_never_confirmed():
    diff = {"added": [], "removed": [], "modified": [], "usage_trend": [
        {"vpsid": 1, "old_used_gb": "100", "new_used_gb": "5"}]}
    events = eh.derive_events(diff, no_prior, "self", ledger_conn=None)
    assert events[0]["event_type"] == eh.EVENT_POSSIBLE_RESET
    assert events[0]["confidence"] == eh.POSSIBLE
    assert "NOT sufficient proof" in events[0]["evidence"]


def test_used_decrease_with_corroborating_applied_operation_is_confirmed_reset():
    ledger_conn = sqlite3.connect(":memory:")
    w.ensure_operations_schema(ledger_conn)
    w.record_operation_start(ledger_conn, "op1", "self", 1, "1|0|1", "500")
    w.record_operation_result(ledger_conn, "op1", "APPLIED")

    diff = {"added": [], "removed": [], "modified": [], "usage_trend": [
        {"vpsid": 1, "old_used_gb": "100", "new_used_gb": "5"}]}
    events = eh.derive_events(diff, no_prior, "self", ledger_conn=ledger_conn)
    assert events[0]["event_type"] == eh.EVENT_CONFIRMED_RESET
    assert events[0]["confidence"] == eh.CONFIRMED
    assert "op1" in events[0]["evidence"]


def test_used_decrease_with_ledger_but_no_applied_op_stays_possible():
    ledger_conn = sqlite3.connect(":memory:")
    w.ensure_operations_schema(ledger_conn)
    w.record_operation_start(ledger_conn, "op1", "self", 1, "1|0|1", "500")
    w.record_operation_result(ledger_conn, "op1", "DEFERRED_LOCKED")  # not APPLIED

    diff = {"added": [], "removed": [], "modified": [], "usage_trend": [
        {"vpsid": 1, "old_used_gb": "100", "new_used_gb": "5"}]}
    events = eh.derive_events(diff, no_prior, "self", ledger_conn=ledger_conn)
    assert events[0]["event_type"] == eh.EVENT_POSSIBLE_RESET


def test_persist_and_query_events_roundtrip(tmp_path):
    import inventory as inv
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    events = [{"vpsid": 1, "event_type": eh.EVENT_VPS_CREATED, "confidence": eh.CONFIRMED,
               "old_value": None, "new_value": None, "evidence": "test"}]
    eh.persist_events(conn, "self", events, "2026-01-01T00:00:00+00:00")
    rows = eh.query_events(conn, "self")
    assert len(rows) == 1
    assert rows[0]["event_type"] == eh.EVENT_VPS_CREATED
    assert rows[0]["vpsid"] == 1


def test_query_events_filters_by_vpsid(tmp_path):
    import inventory as inv
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    eh.persist_events(conn, "self", [
        {"vpsid": 1, "event_type": eh.EVENT_VPS_CREATED, "confidence": eh.CONFIRMED,
         "old_value": None, "new_value": None, "evidence": "a"},
        {"vpsid": 2, "event_type": eh.EVENT_VPS_CREATED, "confidence": eh.CONFIRMED,
         "old_value": None, "new_value": None, "evidence": "b"},
    ], "2026-01-01T00:00:00+00:00")
    rows = eh.query_events(conn, "self", vpsid=2)
    assert len(rows) == 1
    assert rows[0]["vpsid"] == 2


def test_ingest_snapshot_integration_persists_events(tmp_path):
    """Full integration: ingest_snapshot itself derives and persists events automatically."""
    import inventory as inv
    conn = inv.open_db(str(tmp_path / "inv.sqlite3"))
    rec1 = {"vpsid": 1, "hostname": "h", "status": "unknown", "quota_gb": "100", "used_gb": "10",
            "ram_mb": "1024", "cores": "1", "disk_gb": "20", "virtualization": "kvm",
            "locked": "unavailable", "node_serid": "unavailable", "ip_primary": "unavailable",
            "ip_count": "unavailable"}
    result = inv.ingest_snapshot(conn, "self", {"complete": True, "records": [rec1],
                                                 "skipped_count": 0, "error": None,
                                                 "authoritative_total": 1})
    assert result["events"][0]["event_type"] == eh.EVENT_VPS_CREATED
    stored = eh.query_events(conn, "self")
    assert len(stored) == 1
