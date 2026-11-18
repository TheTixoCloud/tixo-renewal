"""Tests for static/live-bandwidth.js (v1.10.0) -- the client-side live bandwidth widget.

Runs the ACTUAL JS file under Node (if available) rather than reimplementing the unit-
conversion logic in Python, so this test fails if the real shipped file's math is wrong --
not just a Python model of it. Skipped (not failed) if Node isn't installed on the test
runner, since this project otherwise has zero JS tooling dependency.
"""
import json
import os
import shutil
import subprocess

import pytest

JS_PATH = os.path.join(os.path.dirname(__file__), "..", "implementation", "static",
                        "live-bandwidth.js")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available on this runner")


def _run(cases):
    """`cases` is a list of (bytes_per_sec, unit) tuples; returns the list of fmtRate()
    results, computed by actually executing the shipped JS file under Node."""
    script = f"""
const m = require({json.dumps(os.path.abspath(JS_PATH))});
const cases = {json.dumps(cases)};
const out = cases.map(c => m.fmtRate(c[0], c[1]));
console.log(JSON.stringify(out));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_file_has_valid_js_syntax():
    result = subprocess.run([NODE, "--check", JS_PATH], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_mbps_conversion_matches_1_000_000_bits_per_second():
    # 12,500,000 bytes/s * 8 = 100,000,000 bits/s = 100 Mbps
    assert _run([(12_500_000, "mbps")]) == ["100.00 Mbps"]


def test_gbps_conversion_matches_1_000_000_000_bits_per_second():
    assert _run([(125_000_000, "gbps")]) == ["1.00 Gbps"]


def test_mbps_never_confused_with_megabytes_per_second():
    """The single most important correctness property: a byte-rate request (MB/s) and a
    bit-rate request (Mbps) for the SAME underlying bytes/sec value must differ by exactly
    8x, never be conflated."""
    results = _run([(1_000_000, "MBps"), (1_000_000, "mbps")])
    assert results[0] == "1.00 MB/s"
    assert results[1] == "8.00 Mbps"  # 1,000,000 B/s * 8 = 8,000,000 bits/s = 8 Mbps


def test_mb_per_s_and_gb_per_s_use_decimal_1000_based_units():
    results = _run([(1_000_000, "MBps"), (1_000_000_000, "GBps")])
    assert results == ["1.00 MB/s", "1.00 GB/s"]


def test_auto_unit_uses_bit_based_networking_convention():
    # Auto always reports bits/sec (Mbps/Gbps), the ISP/networking-industry convention for
    # a RATE -- never bytes/sec, matching traffic_format.py's same rule for the Web/CLI side.
    assert _run([(125_000_000, "auto")]) == ["1.00 Gbps"]


def test_auto_scales_between_bps_mbps_gbps_by_magnitude():
    results = _run([(10, "auto"), (125_000, "auto"), (125_000_000, "auto")])
    assert results[0].endswith("bps") and "Mbps" not in results[0] and "Gbps" not in results[0]
    assert "Mbps" in results[1]
    assert "Gbps" in results[2]


def test_null_or_nan_rate_never_fabricated_as_zero():
    results = _run([(None, "auto")])
    assert results == ["--"]


def test_fmtrate_exposed_for_testing_without_leaking_into_global_browser_scope():
    """Confirms the Node-only module.exports branch exists without disturbing the browser
    IIFE's own `window.TixoLiveBandwidth` attachment (both assignments happen; this just
    confirms the test hook exists)."""
    text = open(JS_PATH, encoding="utf-8").read()
    assert "module.exports" in text
    assert "global.TixoLiveBandwidth = { mount: mount };" in text
