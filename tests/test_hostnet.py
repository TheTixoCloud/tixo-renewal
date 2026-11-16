import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import hostnet as hn


def test_first_sample_has_no_delta_and_starts_cumulative_at_zero(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    result = hn.record_sample(conn, "eth0", 1000, 500, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    assert result["rx_delta"] is None
    assert result["cumulative_rx_bytes"] == 0
    assert result["cumulative_tx_bytes"] == 0
    assert result["discontinuity_type"] is None


def test_normal_increase_computes_delta_and_accumulates(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 1000, 500, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    r2 = hn.record_sample(conn, "eth0", 1500, 800, boot_id="boot-a", sampled_at="2026-01-01T00:05:00+00:00")
    assert r2["rx_delta"] == 500
    assert r2["tx_delta"] == 300
    assert r2["cumulative_rx_bytes"] == 500
    assert r2["cumulative_tx_bytes"] == 300
    assert r2["discontinuity_type"] is None
    assert r2["gap_seconds"] == 300.0


def test_cumulative_grows_across_multiple_samples(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    hn.record_sample(conn, "eth0", 200, 80, boot_id="b", sampled_at="2026-01-01T00:05:00+00:00")
    r3 = hn.record_sample(conn, "eth0", 350, 120, boot_id="b", sampled_at="2026-01-01T00:10:00+00:00")
    assert r3["cumulative_rx_bytes"] == 250  # 100 + 150
    assert r3["cumulative_tx_bytes"] == 70   # 30 + 40


def test_reboot_detected_by_different_boot_id_credits_current_reading_as_delta(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 900000, 400000, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    r2 = hn.record_sample(conn, "eth0", 500, 200, boot_id="boot-b", sampled_at="2026-01-01T01:00:00+00:00")
    assert r2["discontinuity_type"] == hn.DISCONTINUITY_REBOOT
    # the post-reboot counter itself (500/200) is the real total since boot -- credited, not invented
    assert r2["rx_delta"] == 500
    assert r2["tx_delta"] == 200
    assert r2["cumulative_rx_bytes"] == 500


def test_never_computes_negative_delta_same_boot_low_counter_marks_unknown(tmp_path):
    """A counter decrease within the SAME boot (no reboot) that isn't near a 32-bit rollover
    point must never produce a negative or guessed delta -- classified UNKNOWN, delta=None,
    nothing added to cumulative."""
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 10000, 5000, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    r2 = hn.record_sample(conn, "eth0", 300, 100, boot_id="boot-a", sampled_at="2026-01-01T00:05:00+00:00")
    assert r2["discontinuity_type"] == hn.DISCONTINUITY_UNKNOWN
    assert r2["rx_delta"] is None
    assert r2["tx_delta"] is None
    assert r2["cumulative_rx_bytes"] == 0  # unchanged from before -- nothing invented


def test_rollover_near_32bit_boundary_classified_as_rollover_not_unknown(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    near_wrap = 2**32 - 100
    hn.record_sample(conn, "eth0", near_wrap, near_wrap, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    r2 = hn.record_sample(conn, "eth0", 50, 50, boot_id="boot-a", sampled_at="2026-01-01T00:05:00+00:00")
    assert r2["discontinuity_type"] == hn.DISCONTINUITY_ROLLOVER
    assert r2["rx_delta"] is None  # still never guessed, just classified differently


def test_does_not_assume_every_decrease_is_a_reboot(tmp_path):
    """Explicit requirement: a same-boot_id decrease must be classified UNKNOWN/ROLLOVER,
    never silently relabeled REBOOT just because the counter went down."""
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 10000, 5000, boot_id="same-boot", sampled_at="2026-01-01T00:00:00+00:00")
    r2 = hn.record_sample(conn, "eth0", 300, 100, boot_id="same-boot", sampled_at="2026-01-01T00:05:00+00:00")
    assert r2["discontinuity_type"] != hn.DISCONTINUITY_REBOOT


def test_separate_interfaces_tracked_independently(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    hn.record_sample(conn, "eth1", 999, 999, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    r_eth0 = hn.record_sample(conn, "eth0", 200, 80, boot_id="b", sampled_at="2026-01-01T00:05:00+00:00")
    assert r_eth0["cumulative_rx_bytes"] == 100
    assert hn.get_cumulative(conn, "eth1")["cumulative_rx_bytes"] == 0


def test_duplicate_sample_same_timestamp_same_values_produces_zero_delta(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    r2 = hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at="2026-01-01T00:00:01+00:00")
    assert r2["rx_delta"] == 0
    assert r2["tx_delta"] == 0


def test_get_cumulative_none_when_never_sampled(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    assert hn.get_cumulative(conn, "eth0") is None


def test_compute_live_throughput_none_with_fewer_than_two_samples(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    assert hn.compute_live_throughput(conn, "eth0") is None


def test_compute_live_throughput_normal_case(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 1000, 500, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    hn.record_sample(conn, "eth0", 11000, 2500, boot_id="b", sampled_at="2026-01-01T00:00:10+00:00")
    rate = hn.compute_live_throughput(conn, "eth0")
    assert rate["rx_bytes_per_sec"] == 1000.0
    assert rate["tx_bytes_per_sec"] == 200.0


def test_compute_live_throughput_none_across_a_discontinuity(tmp_path):
    """Must never compute a rate across a reboot/reset -- that number would be meaningless."""
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 900000, 400000, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    hn.record_sample(conn, "eth0", 500, 200, boot_id="boot-b", sampled_at="2026-01-01T01:00:00+00:00")
    assert hn.compute_live_throughput(conn, "eth0") is None


def test_sum_deltas_excludes_discontinuity_samples_never_invents_missing_span(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 100, 50, boot_id="boot-a", sampled_at="2026-01-01T00:00:00+00:00")
    hn.record_sample(conn, "eth0", 200, 80, boot_id="boot-a", sampled_at="2026-01-01T00:05:00+00:00")
    hn.record_sample(conn, "eth0", 50, 20, boot_id="boot-b", sampled_at="2026-01-01T01:00:00+00:00")  # reboot
    samples = hn.get_samples_in_range(conn, "eth0", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
    totals = hn.sum_deltas(samples)
    # first sample contributes no delta (None), second contributes 100/30, third (reboot) contributes 50/20
    assert totals["rx_bytes"] == 150
    assert totals["tx_bytes"] == 50
    assert totals["partial"] is True  # the very first sample had no delta


def test_schema_migration_idempotent_repeated_open(tmp_path):
    path = str(tmp_path / "hostnet.sqlite3")
    hn.open_db(path).close()
    hn.open_db(path).close()
    conn = hn.open_db(path)
    row = conn.execute("SELECT value FROM hostnet_meta WHERE key='schema_version'").fetchone()
    assert row[0] == str(hn.SCHEMA_VERSION)


def test_get_boot_id_never_raises():
    result = hn.get_boot_id()
    assert isinstance(result, str)


def test_last_n_days_totals_rolling_window(tmp_path):
    from datetime import datetime, timezone, timedelta
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at=(now - timedelta(days=10)).isoformat())
    hn.record_sample(conn, "eth0", 300, 150, boot_id="b", sampled_at=(now - timedelta(days=3)).isoformat())
    hn.record_sample(conn, "eth0", 500, 250, boot_id="b", sampled_at=now.isoformat())
    totals = hn.last_n_days_totals(conn, "eth0", days=7, now=now)
    # only the last two samples (3 days ago and now) fall inside the 7-day window
    assert totals["rx_bytes"] == 200
    assert totals["tx_bytes"] == 100


def test_current_calendar_period_totals_marked_incomplete(tmp_path):
    conn = hn.open_db(str(tmp_path / "hostnet.sqlite3"))
    hn.record_sample(conn, "eth0", 100, 50, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")
    hn.record_sample(conn, "eth0", 300, 150, boot_id="b", sampled_at="2026-01-05T00:00:00+00:00")
    from datetime import datetime, timezone
    totals = hn.current_calendar_period_totals(
        conn, "eth0", "2026-01-01T00:00:00+00:00", now=datetime(2026, 1, 5, 0, 0, 1, tzinfo=timezone.utc))
    assert totals["complete"] is False
    assert totals["rx_bytes"] == 200
