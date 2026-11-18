"""Tests for charts.py (v1.9.0) -- the pure-stdlib SVG bar-chart renderer used by the Web
Traffic Analytics / Network / Overview pages."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import charts


def _series(n, partial_days=()):
    out = []
    for i in range(n):
        out.append({
            "date": f"2026-01-{i + 1:02d}",
            "rx_bytes": 1_000_000_000 + i * 10_000_000,
            "tx_bytes": 500_000_000 + i * 5_000_000,
            "combined_bytes": 1_500_000_000 + i * 15_000_000,
            "sample_count": 0 if i in partial_days else 10,
            "partial": i in partial_days,
        })
    return out


def test_empty_series_returns_empty_state_not_svg():
    result = charts.render_daily_bar_chart([])
    assert "<svg" not in result
    assert "empty-state" in result


def test_all_zero_sample_count_returns_empty_state():
    series = _series(3, partial_days=(0, 1, 2))
    result = charts.render_daily_bar_chart(series)
    assert "<svg" not in result


def test_normal_series_renders_svg_with_bars_for_every_day():
    series = _series(7)
    result = charts.render_daily_bar_chart(series)
    assert "<svg" in result
    assert result.count("<rect") >= 14  # 2 bars (rx+tx) per day minimum


def test_partial_day_gets_reduced_opacity():
    series = _series(5, partial_days=(2,))
    result = charts.render_daily_bar_chart(series)
    assert charts._PARTIAL_OPACITY in result


def test_tooltips_use_shared_formatting_layer_not_raw_bytes():
    series = _series(3)
    result = charts.render_daily_bar_chart(series)
    assert "1000000000" not in result  # never a raw byte count in the tooltip
    assert "GB" in result or "MB" in result


def test_sparkline_requires_at_least_two_points():
    assert "empty-state" in charts.render_live_sparkline([5.0])
    assert "<svg" in charts.render_live_sparkline([1.0, 2.0, 3.0])
