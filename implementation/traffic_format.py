"""Shared, tested traffic/unit formatting layer (v1.9.0) -- the ONE place both the CLI
(menu.py) and the Web Control Center (web_server.py) convert raw byte counts/rates into
human-readable strings. Extracted from menu.py's pre-existing (v1.7.3) formatting helpers
rather than rewritten from scratch, so existing, already-correct behavior (and its
regression-tested rounding fix) is preserved exactly, just no longer private to the CLI.

UNIT CONVENTION (documented explicitly, never silent):
  - Storage-style totals (cumulative download/upload/combined, VPS quota/used) use DECIMAL
    (SI) units: 1 KB = 1000 bytes, 1 MB = 1000 KB, 1 GB = 1000 MB, 1 TB = 1000 GB --
    matching hosting-industry billing convention and Virtualizor's own GB-denominated
    quotas. This is NEVER the binary KiB/MiB/GiB/TiB (1024-based) a kernel sometimes
    reports; the two differ by ~7% at this scale and this project has never used binary
    units for traffic anywhere.
  - Transfer RATES (bytes/sec from kernel counters) are converted to BITS/sec for display
    (1 byte = 8 bits, always) and shown in bps/Kbps/Mbps/Gbps/Tbps -- matching how ISPs and
    hosting providers always advertise bandwidth. A byte-based rate (KB/s, MB/s) is also
    available for the accounting/transfer-size use case, never confused with the bit rate.
  - "Combined" always means RX + TX of the SAME reading at the SAME point in time/interval
    -- never a sum across different interfaces (e.g. physical NIC + bridge) and never a sum
    of overlapping time windows. See `combine()` below.
"""
from __future__ import annotations

DECIMAL_BYTE_UNITS = (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3))
_BIT_RATE_UNITS = (("Tbps", 1e12), ("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3))
_BYTE_RATE_UNITS = (("TB/s", 1e12), ("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3))


def format_bytes(num_bytes) -> str:
    """Formats a byte COUNT (not a rate) using decimal (SI) units -- "1.42 TB", "836.50 GB",
    "72.40 MB", "512 B". Returns 'unavailable' for None/non-numeric/negative input (never
    fabricates a value)."""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "unavailable"
    if n < 0:
        return "unavailable"
    for unit, divisor in DECIMAL_BYTE_UNITS:
        if n >= divisor:
            return f"{n / divisor:.2f} {unit}"
    return f"{n:.0f} B"


def _scale_rate_human(value: float, units: tuple, base_unit: str) -> str:
    """Shared decimal (1000-based) SI auto-scaling for a non-negative rate -- used by both
    the bit-based and byte-based rate formatters so they can never disagree on scaling
    logic. `units` must be ordered LARGEST divisor first.

    NO MISLEADING ROUNDING (v1.7.3): rounding `value/divisor` to 2 decimals can push it to
    exactly the next tier's threshold (e.g. 999999 B/s -> 999.999 KB/s -> rounds to
    "1000.00 KB/s", which is wrong -- it should display as "1.00 MB/s"). Detected by
    checking whether the ROUNDED value would read >= 1000 and, if so, re-rendering against
    the next larger unit instead."""
    for idx, (unit, divisor) in enumerate(units):
        if value < divisor:
            continue
        scaled = value / divisor
        if round(scaled, 2) >= 1000 and idx > 0:
            bigger_unit, bigger_divisor = units[idx - 1]
            return f"{value / bigger_divisor:.2f} {bigger_unit}"
        return f"{scaled:.2f} {unit}"
    return f"{value:.0f} {base_unit}"


def format_bits_per_second(bytes_per_second) -> str:
    """Converts a byte/sec RATE to a human bit-rate string -- "14.8 Mbps", "2.3 Gbps".
    1 byte = 8 bits, always. Returns 'unavailable' for None/non-numeric/negative input."""
    try:
        bps = float(bytes_per_second) * 8
    except (TypeError, ValueError):
        return "unavailable"
    if bps < 0:
        return "unavailable"
    return _scale_rate_human(bps, _BIT_RATE_UNITS, "bps")


def format_bytes_per_second(bytes_per_second) -> str:
    """B/s -> KB/s -> MB/s -> GB/s -> TB/s, auto-scaled (decimal/SI, matching
    `format_bytes`) -- the transfer/accounting convention, distinct from the bit rate."""
    try:
        n = float(bytes_per_second)
    except (TypeError, ValueError):
        return "unavailable"
    if n < 0:
        return "unavailable"
    return _scale_rate_human(n, _BYTE_RATE_UNITS, "B/s")


def format_dual_rate(bytes_per_second) -> tuple:
    """Returns (bit_based_str, byte_based_str) for the SAME underlying bytes_per_second
    reading: bit-based is the networking-industry convention (ISP/NIC line rates),
    byte-based is the transfer/accounting convention (file sizes, quota consumption).
    Always derived from the exact same raw reading via the fixed 1 byte = 8 bits
    relationship -- never two independently-sampled values that could drift apart."""
    return format_bits_per_second(bytes_per_second), format_bytes_per_second(bytes_per_second)


def combine(rx, tx):
    """Returns rx+tx as a float, or None if either side is missing/negative -- a missing
    side is NEVER silently treated as zero (that would understate a real reading as a
    smaller-looking 'combined' value). Callers format the result themselves (via
    `format_bytes`/`format_bits_per_second` as appropriate) -- this function only combines,
    never formats, so it works for both totals and rates."""
    if rx is None or tx is None:
        return None
    try:
        rx_f, tx_f = float(rx), float(tx)
    except (TypeError, ValueError):
        return None
    if rx_f < 0 or tx_f < 0:
        return None
    return rx_f + tx_f


def format_traffic_block(rx_bytes, tx_bytes) -> dict:
    """The canonical {'download': str, 'upload': str, 'combined': str} shape every
    Download/Upload/Combined block (cumulative totals) in this project should use --
    'combined' is ALWAYS rx+tx of this exact pair (see `combine()`), reported
    'unavailable' rather than silently treating a missing side as zero."""
    combined = combine(rx_bytes, tx_bytes)
    return {
        "download": format_bytes(rx_bytes),
        "upload": format_bytes(tx_bytes),
        "combined": format_bytes(combined) if combined is not None else "unavailable",
    }


def format_rate_block(rx_bytes_per_sec, tx_bytes_per_sec) -> dict:
    """Same shape as `format_traffic_block` but for a live/instantaneous RATE, displayed as
    bit-rates (the networking convention for a rate, vs. bytes for a cumulative total)."""
    combined = combine(rx_bytes_per_sec, tx_bytes_per_sec)
    return {
        "download": format_bits_per_second(rx_bytes_per_sec),
        "upload": format_bits_per_second(tx_bytes_per_sec),
        "combined": format_bits_per_second(combined) if combined is not None else "unavailable",
    }
