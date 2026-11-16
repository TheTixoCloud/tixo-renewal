import os
import sqlite3
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import analytics
import writer as w


def rec(vpsid, quota, used, status="running"):
    return {"vpsid": vpsid, "quota_gb": quota, "used_gb": used, "status": status}


def test_unlimited_vps_excluded_from_limited_sums():
    records = [rec(1, "100", "50"), rec(2, "0", "9999")]  # vpsid 2 is unlimited
    result = analytics.compute_bandwidth_analytics(records)
    assert result["limited_vps_count"] == 1
    assert result["unlimited_vps_count"] == 1
    assert result["limited_quota_sum_gb"] == Decimal("100")
    assert result["limited_used_sum_gb"] == Decimal("50")


def test_negative_quota_also_treated_as_unlimited():
    records = [rec(1, "-1", "10")]
    result = analytics.compute_bandwidth_analytics(records)
    assert result["unlimited_vps_count"] == 1
    assert result["limited_vps_count"] == 0


def test_missing_used_bandwidth_excluded_from_sum_not_treated_as_zero():
    records = [rec(1, "100", analytics.UNAVAILABLE), rec(2, "100", "30")]
    result = analytics.compute_bandwidth_analytics(records)
    assert result["limited_used_sum_gb"] == Decimal("30")
    assert result["limited_used_sum_coverage"] == "1/2"


def test_unknown_quota_vps_counted_separately_not_as_limited_or_unlimited():
    records = [rec(1, analytics.UNAVAILABLE, "10")]
    result = analytics.compute_bandwidth_analytics(records)
    assert result["unknown_quota_vps_count"] == 1
    assert result["limited_vps_count"] == 0
    assert result["unlimited_vps_count"] == 0


def test_running_and_stopped_counts():
    records = [rec(1, "100", "10", status="running"), rec(2, "100", "10", status="stopped"),
               rec(3, "100", "10", status="suspended"), rec(4, "100", "10", status="unknown")]
    result = analytics.compute_bandwidth_analytics(records)
    assert result["running_count"] == 1
    assert result["stopped_count"] == 1
    assert result["suspended_count"] == 1
    assert result["unknown_status_count"] == 1


def test_usage_threshold_flags_vps_over_limit():
    records = [rec(1, "100", "95"), rec(2, "100", "10")]
    result = analytics.compute_bandwidth_analytics(records, usage_threshold_percent=Decimal("90"))
    assert result["vps_over_threshold"] == [1]


def test_top_consumers_sorted_descending():
    records = [rec(1, "100", "10"), rec(2, "100", "90"), rec(3, "100", "50")]
    result = analytics.compute_bandwidth_analytics(records)
    assert [c["vpsid"] for c in result["top_consumers"]] == [2, 3, 1]


def test_unlimited_used_sum_reported_separately_when_measurable():
    records = [rec(1, "0", "500"), rec(2, "0", "300")]
    result = analytics.compute_bandwidth_analytics(records)
    assert result["unlimited_used_sum_gb"] == Decimal("800")


def test_unlimited_used_sum_unavailable_when_no_data():
    records = [rec(1, "0", analytics.UNAVAILABLE)]
    result = analytics.compute_bandwidth_analytics(records)
    assert result["unlimited_used_sum_gb"] == analytics.UNAVAILABLE


def test_compute_renewal_outcome_counts_reads_existing_ledger(tmp_path):
    conn = sqlite3.connect(":memory:")
    w.ensure_operations_schema(conn)
    w.record_operation_start(conn, "op1", "self", 1, "1|0|1", "100")
    w.record_operation_result(conn, "op1", "APPLIED")
    w.record_operation_start(conn, "op2", "self", 2, "2|0|1", "100")
    w.record_operation_result(conn, "op2", "DEFERRED_LOCKED")
    counts = analytics.compute_renewal_outcome_counts(conn, "self")
    assert counts["applied"] == 1
    assert counts["deferred_locked"] == 1
    assert counts["unknown_needs_verification"] == 0
