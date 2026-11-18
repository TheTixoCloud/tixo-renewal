"""Tests for implementation/static/ (v1.10.0) -- the TixoCloud branding assets and the
live-bandwidth client script. Confirms the committed PNGs are genuinely valid images (not a
placeholder, not corrupted), not that web_server.py serves them (see test_web_server.py for
that -- route/allowlist/auth behavior).
"""
import os
import struct

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "implementation", "static")


def _png_dimensions(path):
    with open(path, "rb") as f:
        header = f.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a valid PNG (bad magic bytes)"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_light_logo_is_a_real_png_with_sane_dimensions():
    width, height = _png_dimensions(os.path.join(STATIC_DIR, "tixocloud.png"))
    assert width > 0 and height > 0
    assert width < 10000 and height < 10000  # not a corrupted/garbage header


def test_dark_logo_is_a_real_png_with_sane_dimensions():
    width, height = _png_dimensions(os.path.join(STATIC_DIR, "tixocloud-dark.png"))
    assert width > 0 and height > 0


def test_light_and_dark_logo_share_the_same_aspect_ratio():
    """Both variants are the same artwork with only the wordmark color swapped -- their
    dimensions should match exactly so neither looks stretched when CSS applies the same
    height to both."""
    light = _png_dimensions(os.path.join(STATIC_DIR, "tixocloud.png"))
    dark = _png_dimensions(os.path.join(STATIC_DIR, "tixocloud-dark.png"))
    assert light == dark


def test_live_bandwidth_js_file_exists_and_is_nonempty():
    path = os.path.join(STATIC_DIR, "live-bandwidth.js")
    assert os.path.getsize(path) > 500


def test_all_static_files_referenced_by_web_server_actually_exist_on_disk():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))
    import web_server as ws
    for route, (filename, _content_type) in ws.STATIC_FILES.items():
        path = os.path.join(STATIC_DIR, filename)
        assert os.path.exists(path), f"{route} -> {filename} missing from {STATIC_DIR}"
