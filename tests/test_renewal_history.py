import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import renewal_history as rh
import shadow_monitor as sm
import writer as w


def make_ledger_conn(tmp_path):
    conn = sm.open_db(str(tmp_path / "self.sqlite3"))
    return conn


def test_monitoring_coverage_start_from_identity_first_seen(tmp_path):
    conn = make_ledger_conn(tmp_path)
    sm.ingest_poll(conn, [{"vpsid": 1, "created_epoch": 1000, "quota_gb": "100", "used_gb": "10"}],
                   "2026-01-01T00:00:00+00:00")
    history = rh.get_vps_renewal_history(conn, "self", 1)
    assert history["monitoring_coverage_start"] == "2026-01-01T00:00:00+00:00"


def test_no_identity_means_unknown_coverage_start(tmp_path):
    conn = make_ledger_conn(tmp_path)
    history = rh.get_vps_renewal_history(conn, "self", 999)
    assert history["monitoring_coverage_start"] is None


def test_confirmed_renewals_and_resets_count_applied_operations(tmp_path):
    conn = make_ledger_conn(tmp_path)
    w.record_operation_start(conn, "op1", "self", 1, "1|0|1", "500")
    w.record_operation_result(conn, "op1", "APPLIED")
    w.record_operation_start(conn, "op2", "self", 1, "1|0|2", "500")
    w.record_operation_result(conn, "op2", "APPLIED")
    history = rh.get_vps_renewal_history(conn, "self", 1)
    assert history["confirmed_renewals"] == 2
    assert history["confirmed_resets"] == 2
    assert history["last_confirmed_renewal_at"] is not None


def test_deferred_expired_and_failed_counted_separately(tmp_path):
    conn = make_ledger_conn(tmp_path)
    w.record_operation_start(conn, "op1", "self", 1, "1|0|1", "500")
    w.record_operation_result(conn, "op1", "DEFERRED_LOCKED")
    w.record_operation_start(conn, "op2", "self", 1, "1|0|2", "500")
    w.record_operation_result(conn, "op2", "DEFERRED_EXPIRED_NEEDS_REVIEW")
    w.record_operation_start(conn, "op3", "self", 1, "1|0|3", "500")
    w.record_operation_result(conn, "op3", "UNKNOWN_NEEDS_VERIFICATION")
    history = rh.get_vps_renewal_history(conn, "self", 1)
    assert history["deferred_attempts"] == 1
    assert history["expired_deferrals"] == 1
    assert history["failed_or_unknown_operations"] == 1
    assert history["confirmed_renewals"] == 0


def test_migration_count_from_inventory_events_when_provided(tmp_path):
    import inventory as inv
    import event_history as eh
    ledger_conn = make_ledger_conn(tmp_path)
    inv_conn = inv.open_db(str(tmp_path / "inventory.sqlite3"))
    eh.persist_events(inv_conn, "self", [
        {"vpsid": 1, "event_type": eh.EVENT_VPS_MIGRATION, "confidence": eh.CONFIRMED,
         "old_value": "1", "new_value": "2", "evidence": "x"},
    ], "2026-01-01T00:00:00+00:00")
    history = rh.get_vps_renewal_history(ledger_conn, "self", 1, inventory_conn=inv_conn)
    assert history["migration_count"] == 1


def test_migration_count_none_when_inventory_conn_not_provided(tmp_path):
    conn = make_ledger_conn(tmp_path)
    history = rh.get_vps_renewal_history(conn, "self", 1)
    assert history["migration_count"] is None
