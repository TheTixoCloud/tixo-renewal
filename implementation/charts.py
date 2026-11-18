"""Pure-stdlib SVG chart rendering (v1.9.0) -- zero external charting library, zero CDN,
zero JS framework, matching this project's zero-third-party-dependency rule. Produces a
single inline <svg> string the Web pages embed directly; the only "interactivity" is a
native <title> tooltip on each bar (hover text any browser renders for free) and the
page wrapping it in a horizontally-scrollable `.chart-wrap` div for small screens.

HONESTY RULES (matching traffic_format.py's "never fabricate" discipline):
  - A day/period with zero recorded samples is rendered as a zero-height bar, never
    omitted or interpolated -- omitting it would visually imply a shorter/different time
    range than actually requested.
  - A day marked `partial` (a discontinuity sample, a reboot, or literally zero samples)
    gets a visibly different (hatched/dimmer) bar fill and a tooltip note -- never rendered
    identically to a fully-sampled day.
  - All values are passed through `traffic_format.format_bytes` for tooltips/axis labels --
    this module never invents its own unit scaling.
"""
from __future__ import annotations

import html

import traffic_format as tf

_RX_COLOR = "#3b82f6"
_TX_COLOR = "#ef4444"
_PARTIAL_OPACITY = "0.35"


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def render_daily_bar_chart(series: list[dict], width: int = 640, height: int = 220,
                            title: str = "") -> str:
    """Grouped RX/TX bar chart, one group per day, oldest-to-newest left-to-right.
    `series` is the exact shape returned by traffic_analytics.daily_series(): a list of
    {"date", "rx_bytes", "tx_bytes", "combined_bytes", "sample_count", "partial"}.
    Returns an empty-state string (no <svg>) if there is no real data to show -- never a
    chart that looks populated but is actually all zeros from missing monitoring."""
    if not series or all(d.get("sample_count", 0) == 0 for d in series):
        return '<div class="empty-state">No historical samples yet for this range.</div>'

    pad_left, pad_right, pad_top, pad_bottom = 46, 10, 18, 28
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(series)
    max_val = max((max(d["rx_bytes"], d["tx_bytes"]) for d in series), default=0) or 1

    group_w = plot_w / n
    bar_w = max(group_w * 0.32, 1)

    bars = []
    x_labels = []
    for i, d in enumerate(series):
        group_x = pad_left + i * group_w
        rx_h = (d["rx_bytes"] / max_val) * plot_h
        tx_h = (d["tx_bytes"] / max_val) * plot_h
        opacity = _PARTIAL_OPACITY if d.get("partial") else "1"
        rx_title = f"{d['date']}: Download {tf.format_bytes(d['rx_bytes'])}" + (" (partial coverage)" if d.get("partial") else "")
        tx_title = f"{d['date']}: Upload {tf.format_bytes(d['tx_bytes'])}" + (" (partial coverage)" if d.get("partial") else "")
        rx_x = group_x + group_w * 0.12
        tx_x = group_x + group_w * 0.56
        bars.append(
            f'<rect x="{rx_x:.1f}" y="{pad_top + plot_h - rx_h:.1f}" width="{bar_w:.1f}" '
            f'height="{max(rx_h, 0.5):.1f}" fill="{_RX_COLOR}" opacity="{opacity}" rx="2">'
            f'<title>{_esc(rx_title)}</title></rect>'
        )
        bars.append(
            f'<rect x="{tx_x:.1f}" y="{pad_top + plot_h - tx_h:.1f}" width="{bar_w:.1f}" '
            f'height="{max(tx_h, 0.5):.1f}" fill="{_TX_COLOR}" opacity="{opacity}" rx="2">'
            f'<title>{_esc(tx_title)}</title></rect>'
        )
        if n <= 10 or i % max(1, n // 10) == 0:
            label = d["date"][5:]  # MM-DD, local calendar day already (see daily_series)
            x_labels.append(
                f'<text x="{group_x + group_w / 2:.1f}" y="{height - 8}" font-size="9.5" '
                f'text-anchor="middle" fill="var(--muted)">{_esc(label)}</text>'
            )

    gridlines = []
    y_labels = []
    for frac in (0.0, 0.5, 1.0):
        y = pad_top + plot_h * (1 - frac)
        gridlines.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="var(--border)" stroke-width="1"/>'
        )
        y_labels.append(
            f'<text x="{pad_left - 6}" y="{y + 3:.1f}" font-size="9.5" text-anchor="end" '
            f'fill="var(--muted)">{_esc(tf.format_bytes(max_val * frac))}</text>'
        )

    legend = (
        f'<g font-size="11">'
        f'<rect x="{pad_left}" y="2" width="10" height="10" fill="{_RX_COLOR}"/>'
        f'<text x="{pad_left + 14}" y="11" fill="var(--fg)">Download</text>'
        f'<rect x="{pad_left + 90}" y="2" width="10" height="10" fill="{_TX_COLOR}"/>'
        f'<text x="{pad_left + 104}" y="11" fill="var(--fg)">Upload</text>'
        f'</g>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="{_esc(title)}">'
        f'{legend}{"".join(gridlines)}{"".join(y_labels)}{"".join(bars)}{"".join(x_labels)}'
        f'</svg>'
    )


def render_live_sparkline(points: list[float], width: int = 320, height: int = 48) -> str:
    """A minimal single-series sparkline (e.g. for a short live-rate history) -- no axes, no
    legend, just a trend line. Returns an empty-state string if fewer than 2 points."""
    if len(points) < 2:
        return '<div class="empty-state">Not enough samples yet.</div>'
    max_val = max(points) or 1
    min_val = min(points)
    span = (max_val - min_val) or 1
    step = width / (len(points) - 1)
    coords = []
    for i, v in enumerate(points):
        x = i * step
        y = height - ((v - min_val) / span) * (height - 4) - 2
        coords.append(f"{x:.1f},{y:.1f}")
    path = " ".join(coords)
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
            f'<polyline points="{path}" fill="none" stroke="{_RX_COLOR}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')
