import os

import first_renewal_validation as frv


def test_arm_then_is_armed(tmp_path):
    config_dir = str(tmp_path)
    assert frv.is_armed(config_dir, "self") is False
    frv.arm(config_dir, "self")
    assert frv.is_armed(config_dir, "self") is True


def test_disarm_clears_armed_state(tmp_path):
    config_dir = str(tmp_path)
    frv.arm(config_dir, "self")
    frv.disarm(config_dir, "self")
    assert frv.is_armed(config_dir, "self") is False


def test_disarm_is_a_safe_noop_when_not_armed(tmp_path):
    frv.disarm(str(tmp_path), "self")  # must not raise


def test_record_first_outcome_is_a_noop_when_not_armed(tmp_path):
    config_dir = str(tmp_path)
    record = frv.record_first_outcome(
        config_dir, "self", 42, "42|123|0", "100",
        {"status": "APPLIED", "operation_id": "op1", "verified_after_quota_gb": "100"})
    assert record is None
    assert frv.load_outcomes(config_dir, "self") == []


def test_record_first_outcome_consumes_the_armed_marker_exactly_once(tmp_path):
    """The SECOND real write after authorization must never be mistaken for the first --
    this is the core exactly-once guarantee of the whole mechanism."""
    config_dir = str(tmp_path)
    frv.arm(config_dir, "self")

    first = frv.record_first_outcome(
        config_dir, "self", 42, "42|123|0", "100",
        {"status": "APPLIED", "operation_id": "op1", "verified_after_quota_gb": "100"})
    assert first is not None
    assert frv.is_armed(config_dir, "self") is False

    # A second real renewal (different vpsid/period) after this must NOT be recorded as
    # "the first" again.
    second = frv.record_first_outcome(
        config_dir, "self", 43, "43|456|0", "200",
        {"status": "APPLIED", "operation_id": "op2", "verified_after_quota_gb": "200"})
    assert second is None
    outcomes = frv.load_outcomes(config_dir, "self")
    assert len(outcomes) == 1
    assert outcomes[0]["vpsid"] == 42


def test_record_first_outcome_matching_quota_is_verified_ok(tmp_path):
    config_dir = str(tmp_path)
    frv.arm(config_dir, "self")
    record = frv.record_first_outcome(
        config_dir, "self", 42, "42|123|0", "100",
        {"status": "APPLIED", "operation_id": "op1", "verified_after_quota_gb": "100"})
    assert record["verified_ok"] is True
    assert record["write_status"] == "APPLIED"
    assert record["expected_quota_gb"] == "100"
    assert record["actual_quota_gb"] == "100"


def test_record_first_outcome_mismatch_is_needs_review_not_ok(tmp_path):
    """The exact scenario this mechanism exists to surface: a real write whose live-
    verified quota does NOT match what was expected. This module never retries or alters
    anything -- writer.py's own UNKNOWN_NEEDS_VERIFICATION status (not this module) is what
    already structurally blocks any further automatic attempt for this period."""
    config_dir = str(tmp_path)
    frv.arm(config_dir, "self")
    record = frv.record_first_outcome(
        config_dir, "self", 42, "42|123|0", "100",
        {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": "op1",
         "verified_after_quota_gb": "87"})
    assert record["verified_ok"] is False
    assert record["write_status"] == "UNKNOWN_NEEDS_VERIFICATION"


def test_record_first_outcome_failed_write_with_no_verified_quota_is_needs_review(tmp_path):
    config_dir = str(tmp_path)
    frv.arm(config_dir, "self")
    record = frv.record_first_outcome(
        config_dir, "self", 42, "42|123|0", "100",
        {"status": "FAILED", "operation_id": "op1"})
    assert record["verified_ok"] is False
    assert record["actual_quota_gb"] is None


def test_load_outcomes_survives_a_corrupt_file(tmp_path):
    config_dir = str(tmp_path)
    frv.arm(config_dir, "self")
    frv.record_first_outcome(config_dir, "self", 1, "k", "1",
                              {"status": "APPLIED", "verified_after_quota_gb": "1"})
    path = frv._outcomes_path(config_dir, "self")
    open(path, "w").write("{ not valid json")
    assert frv.load_outcomes(config_dir, "self") == []


def test_outcomes_are_per_node_never_cross_contaminated(tmp_path):
    config_dir = str(tmp_path)
    frv.arm(config_dir, "node-a")
    frv.arm(config_dir, "node-b")
    frv.record_first_outcome(config_dir, "node-a", 1, "k1", "100",
                              {"status": "APPLIED", "verified_after_quota_gb": "100"})
    # node-b is still armed -- node-a's outcome must not have consumed it.
    assert frv.is_armed(config_dir, "node-b") is True
    assert frv.load_outcomes(config_dir, "node-b") == []


def test_build_notification_text_distinguishes_ok_from_needs_review():
    ok_record = {"node_id": "self", "vpsid": 1, "expected_quota_gb": "100",
                 "actual_quota_gb": "100", "operation_id": "op1", "verified_ok": True,
                 "write_status": "APPLIED"}
    bad_record = {"node_id": "self", "vpsid": 1, "expected_quota_gb": "100",
                  "actual_quota_gb": "87", "operation_id": "op1", "verified_ok": False,
                  "write_status": "UNKNOWN_NEEDS_VERIFICATION"}
    assert "VALIDATED" in frv.build_notification_text(ok_record)
    assert "NEEDS REVIEW" in frv.build_notification_text(bad_record)
