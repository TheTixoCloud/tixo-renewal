"""Regression tests for the v1.7.3 dual bit/byte live-speed display (the production task:
'IMPROVE LIVE NETWORK SPEED DISPLAY WITH DUAL BIT/BYTE UNITS'). Covers the enumerated test
categories:
  1. bit/byte conversion (1 byte = 8 bits)
  2. SI (1000-based) unit scaling
  3. boundary values (no misleading rounding across a tier threshold)
  4. zero and sub-Kbps/sub-KB speeds
  5. large Gbps/Tbps and GB/s/TB/s speeds
  6. combined speed calculation
  7. correctly labeled, dual-line output
  8. live refresh / navigation compatibility preserved
  9. no persistent database writes from the live UI
  10. existing renewal and hostnet accounting safety preserved
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import hostnet
import menu


# ---------- 1/2: bit/byte conversion and SI scaling -----------------------------------------

def test_bits_per_sec_is_eight_times_bytes_per_sec_before_scaling():
    # 125 bytes/sec * 8 = 1000 bits/sec = exactly 1.00 Kbps
    assert menu._format_bits_per_sec_human(125) == "1.00 Kbps"


def test_byte_and_bit_formatters_scale_independently_from_the_same_reading():
    bit_str, byte_str = menu._format_dual_speed_human(125_000_000)  # 125 MB/s
    assert byte_str == "125.00 MB/s"
    assert bit_str == "1.00 Gbps"  # 125,000,000 * 8 = 1,000,000,000 bits/sec


def test_si_decimal_scaling_not_binary():
    # 1,000,000 bytes/sec must read as exactly 1.00 MB/s (decimal), not ~0.95 MiB/s (binary)
    assert menu._format_bytes_per_sec_human(1_000_000) == "1.00 MB/s"
    assert menu._format_bits_per_sec_human(1_000_000) == "8.00 Mbps"


# ---------- 3: boundary values / no misleading rounding -------------------------------------

def test_no_misleading_rounding_at_kb_mb_boundary():
    """999999 B/s is just under 1,000,000 -- naive rounding to 2 decimals would show
    '1000.00 KB/s', which is wrong. It must bump to the next tier instead."""
    assert menu._format_bytes_per_sec_human(999999) == "1.00 MB/s"
    assert "1000.00 KB/s" not in menu._format_bytes_per_sec_human(999999)


def test_no_misleading_rounding_at_mb_gb_boundary():
    assert menu._format_bytes_per_sec_human(999_999_000) == "1.00 GB/s"


def test_no_misleading_rounding_for_bits_at_gb_tb_boundary():
    # 124,999,999,999 B/s * 8 = 999,999,999,992 bits/sec, just under 1 Tbps
    result = menu._format_bits_per_sec_human(124_999_999_999)
    assert result == "1.00 Tbps"
    assert "1000.00 Gbps" not in result


def test_exact_tier_boundary_values():
    assert menu._format_bytes_per_sec_human(1000) == "1.00 KB/s"
    assert menu._format_bytes_per_sec_human(1_000_000) == "1.00 MB/s"
    assert menu._format_bytes_per_sec_human(1_000_000_000) == "1.00 GB/s"
    assert menu._format_bytes_per_sec_human(1_000_000_000_000) == "1.00 TB/s"


# ---------- 4: zero and sub-Kbps/sub-KB speeds -----------------------------------------------

def test_zero_speed_never_crashes_and_reads_as_zero():
    assert menu._format_bytes_per_sec_human(0) == "0 B/s"
    assert menu._format_bits_per_sec_human(0) == "0 bps"
    assert menu._format_dual_speed_human(0) == ("0 bps", "0 B/s")


def test_sub_kbps_sub_kb_speed_stays_in_base_unit():
    bit_str, byte_str = menu._format_dual_speed_human(50)  # 50 B/s = 400 bps
    assert byte_str == "50 B/s"
    assert bit_str == "400 bps"


def test_negative_or_unparseable_speed_reports_unavailable_never_fabricated():
    assert menu._format_dual_speed_human(None) == ("unavailable", "unavailable")
    assert menu._format_dual_speed_human("garbage") == ("unavailable", "unavailable")


# ---------- 5: large Gbps/Tbps and GB/s/TB/s speeds ------------------------------------------

def test_large_speed_scales_to_tbps_and_tb_per_sec():
    bit_str, byte_str = menu._format_dual_speed_human(2.5e12)  # 2.5 TB/s
    assert byte_str == "2.50 TB/s"
    assert bit_str == "20.00 Tbps"


def test_gbps_tier_matches_task_illustrative_example():
    """From the task's own illustrative snapshot: ~188.68 MB/s should read as ~1.51 Gbps."""
    bit_str, byte_str = menu._format_dual_speed_human(188.68e6)
    assert byte_str == "188.68 MB/s"
    assert bit_str == "1.51 Gbps"


# ---------- 6: combined speed calculation ----------------------------------------------------

def test_combined_speed_is_sum_of_rx_and_tx_not_an_average():
    rx_bps, tx_bps = 188.68e6, 191.31e6
    combined = rx_bps + tx_bps
    bit_str, byte_str = menu._format_dual_speed_human(combined)
    assert byte_str == "379.99 MB/s"
    assert bit_str == "3.04 Gbps"


# ---------- 7: correctly labeled, dual-line output -------------------------------------------

def test_live_panel_shows_bit_speed_and_byte_speed_for_each_direction():
    out = menu.render_live_hostnet_panel(
        "eth0", {"rx_bytes_per_sec": 188.68e6, "tx_bytes_per_sec": 191.31e6}, True,
        {"cumulative_rx_bytes": 10_000_000_000, "cumulative_tx_bytes": 5_000_000_000},
        {"sampled_at": "2026-01-01T00:00:00+00:00", "discontinuity_type": None},
        menu.Style(enabled=False))
    assert "Live Download (RX):" in out
    assert "Live Upload (TX):" in out
    assert "Live Combined:" in out
    assert "1.51 Gbps" in out
    assert "188.68 MB/s" in out
    assert "3.04 Gbps" in out
    assert "379.99 MB/s" in out
    # the bit-based and byte-based lines must never be collapsed into one line
    rx_section = out.split("Live Download (RX):")[1].split("Live Upload (TX):")[0]
    assert "Gbps" in rx_section and "MB/s" in rx_section


def test_persistent_speed_section_also_shows_dual_units():
    out = menu.render_hostnet_panel(
        "eth0",
        {"cumulative_rx_bytes": 5_000_000_000, "cumulative_tx_bytes": 2_000_000_000,
         "first_sample_at": "2026-01-01T00:00:00+00:00"},
        {"rx_bytes_per_sec": 1_000_000, "tx_bytes_per_sec": 500_000},
        {"sampled_at": "2026-01-02T00:00:00+00:00", "discontinuity_type": None, "gap_seconds": 300},
        menu.Style(enabled=False))
    assert "Persistent Download (RX) Speed:" in out
    assert "Mbps" in out
    assert "MB/s" in out


def test_persistent_totals_remain_byte_only_no_bit_units_mixed_in():
    """Totals (not speeds) must stay byte-based only, per the explicit requirement to never
    mix bit units into cumulative transfer totals."""
    out = menu.render_live_hostnet_panel(
        "eth0", None, False,
        {"cumulative_rx_bytes": 5_000_000_000, "cumulative_tx_bytes": 2_000_000_000},
        None, menu.Style(enabled=False))
    assert "Persistent Download (RX) Total: 5.00 GB" in out
    assert "Gbps" not in out.split("Persistent Download (RX) Total:")[1].split("\n")[0]


# ---------- 8: live refresh / navigation compatibility preserved ----------------------------

def test_live_loop_still_exits_on_q_without_blocking_input(tmp_path, monkeypatch):
    monkeypatch.setattr(menu.hostnet_sync, "load_settings",
                         lambda config_dir: {"interface": "eth0"})
    monkeypatch.setattr(menu, "load_display_tz", lambda config_dir: "UTC")
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(menu, "supports_raw_input", lambda: True)
    monkeypatch.setattr(menu, "read_live_interface_counters", lambda iface: (1000, 2000))
    monkeypatch.setattr(menu, "poll_single_key", lambda timeout: "q")
    monkeypatch.setattr(menu, "clear_screen", lambda: None)

    class FakeGuard:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(menu, "_RawTerminalGuard", FakeGuard)

    with mock.patch("builtins.input") as input_mock:
        menu.action_view_hostnet(menu.Style(enabled=False))
    input_mock.assert_not_called()


# ---------- 9/10: no persistent DB writes from the live UI; accounting safety ---------------

def test_live_panel_render_never_imports_or_calls_record_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(menu.hostnet_sync, "load_settings",
                         lambda config_dir: {"interface": "eth0"})
    monkeypatch.setattr(menu, "load_display_tz", lambda config_dir: "UTC")
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(menu, "supports_raw_input", lambda: True)
    monkeypatch.setattr(menu, "read_live_interface_counters", lambda iface: (5_000_000, 3_000_000))
    monkeypatch.setattr(menu, "poll_single_key", lambda timeout: "0")
    monkeypatch.setattr(menu, "clear_screen", lambda: None)

    class FakeGuard:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(menu, "_RawTerminalGuard", FakeGuard)

    record_sample_mock = mock.Mock()
    monkeypatch.setattr(hostnet, "record_sample", record_sample_mock)
    menu.action_view_hostnet(menu.Style(enabled=False))
    record_sample_mock.assert_not_called()


def test_dual_speed_formatting_does_not_alter_persistent_total_formatter():
    """_format_bytes_human (used for persistent TOTALS, not speeds) is untouched -- same
    byte-only, no-bit-unit behavior as before this change."""
    assert menu._format_bytes_human(5_000_000_000) == "5.00 GB"
    assert menu._format_bytes_human(500) == "500 B"
