"""Tests for traffic_format.py (v1.9.0): the shared, single-source-of-truth unit-formatting
layer used by both the CLI and the Web Control Center. The individual scaling behaviors
(decimal units, no-misleading-rounding fix, dual bit/byte rate) were already covered for
menu.py's own private copies in test_dual_unit_speed_display.py/test_menu.py -- those still
pass unchanged (menu.py now aliases these same functions). This file focuses on the NEW
public surface: `combine()` and the `format_traffic_block`/`format_rate_block` dict shapes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import traffic_format as tf


# ---------- format_bytes / format_bits_per_second / format_bytes_per_second (sanity) -----------

def test_format_bytes_decimal_units():
    assert tf.format_bytes(5_000_000_000) == "5.00 GB"
    assert tf.format_bytes(500) == "500 B"
    assert tf.format_bytes(1_420_000_000_000) == "1.42 TB"


def test_format_bytes_rejects_negative():
    assert tf.format_bytes(-5) == "unavailable"


def test_format_bytes_rejects_none_and_garbage():
    assert tf.format_bytes(None) == "unavailable"
    assert tf.format_bytes("not a number") == "unavailable"


def test_format_bits_per_second_examples():
    assert tf.format_bits_per_second(12_500_000) == "100.00 Mbps"
    assert tf.format_bits_per_second(125_000_000) == "1.00 Gbps"


def test_format_bytes_per_second_examples():
    assert tf.format_bytes_per_second(1_000_000) == "1.00 MB/s"
    assert tf.format_bytes_per_second(1_000_000_000) == "1.00 GB/s"


def test_format_dual_rate_matches_single_measurement():
    bit_str, byte_str = tf.format_dual_rate(125_000_000)  # 125 MB/s
    assert byte_str == "125.00 MB/s"
    assert bit_str == "1.00 Gbps"  # 125 MB/s * 8 = 1000 Mbit/s = 1 Gbit/s


# ---------- combine() ---------------------------------------------------------------------------

def test_combine_adds_both_sides():
    assert tf.combine(100, 200) == 300.0


def test_combine_none_if_either_side_missing():
    assert tf.combine(None, 200) is None
    assert tf.combine(100, None) is None
    assert tf.combine(None, None) is None


def test_combine_none_if_either_side_negative():
    """A negative delta (e.g. an unhandled counter reset) must never be silently combined
    into a plausible-looking positive total."""
    assert tf.combine(-5, 200) is None
    assert tf.combine(100, -5) is None


def test_combine_rejects_garbage():
    assert tf.combine("not a number", 200) is None


# ---------- format_traffic_block (cumulative totals) --------------------------------------------

def test_format_traffic_block_normal_case():
    result = tf.format_traffic_block(5_000_000_000, 3_000_000_000)
    assert result == {"download": "5.00 GB", "upload": "3.00 GB", "combined": "8.00 GB"}


def test_format_traffic_block_missing_side_reports_combined_unavailable():
    result = tf.format_traffic_block(5_000_000_000, None)
    assert result["download"] == "5.00 GB"
    assert result["upload"] == "unavailable"
    assert result["combined"] == "unavailable"  # never silently treated the missing side as 0


def test_format_traffic_block_both_missing():
    result = tf.format_traffic_block(None, None)
    assert result == {"download": "unavailable", "upload": "unavailable", "combined": "unavailable"}


def test_format_traffic_block_zero_is_a_real_value_not_unavailable():
    result = tf.format_traffic_block(0, 0)
    assert result == {"download": "0 B", "upload": "0 B", "combined": "0 B"}


# ---------- format_rate_block (live/instantaneous rates) -----------------------------------------

def test_format_rate_block_normal_case():
    result = tf.format_rate_block(12_500_000, 12_500_000)  # 100 Mbps each way
    assert result["download"] == "100.00 Mbps"
    assert result["upload"] == "100.00 Mbps"
    assert result["combined"] == "200.00 Mbps"


def test_format_rate_block_missing_side_reports_combined_unavailable():
    result = tf.format_rate_block(12_500_000, None)
    assert result["combined"] == "unavailable"


# ---------- no-misleading-rounding regression (shared scaling helper) ---------------------------

def test_no_misleading_rounding_at_tier_boundary():
    # 999999 B/s must never render as "1000.00 KB/s" -- bumps to the next tier instead.
    result = tf.format_bytes_per_second(999999)
    assert result == "1.00 MB/s"
    assert "1000.00 KB/s" not in result
