"""Regression tests for the v1.7.2 live-traffic/navigation/labeling/VPS-status fix (the
production task: 'FIX LIVE HOST TRAFFIC UI, NAVIGATION, TRAFFIC LABELS, AND VPS RUNTIME
STATUS'). Covers the fifteen enumerated test categories from that task where unit-testable
without a real tty or production API access:
  1. live refresh without pressing Enter
  2. speed calculation from sequential samples
  3. counter reset / unavailable-interface behavior
  4. responsive single-key back/quit
  5. terminal restoration after normal exit and after Ctrl+C
  6. SSH/tmux-compatible rendering (graceful fallback when not a tty)
  7. no duplicate/uncontrolled output accumulation
  8. correct Download(RX)/Upload(TX) labeling
  9. VPS status normalization (documented 0/1/2 codes)
  10. missing/unrecognized runtime status fields never fabricated
  11. bulk API coverage (one call for all vpsids, not one per VPS)
  12. read-only API behavior (no mutation-shaped parameters ever sent)
  13. inventory DB migration compatibility (new columns added to an old schema)
  14. renewal ledger/ingest safety preserved (incomplete snapshot still rejected)
  15. persistent hostnet accounting is never touched by the live view
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import hostnet
import inventory as inv
import menu
import virtualizor_info as vinfo


# ---------- 1/4/5/6/7/15: live screen behavior, navigation, terminal safety -----------------

def test_action_view_hostnet_never_calls_blocking_input_when_raw_input_supported(tmp_path, monkeypatch):
    """ROOT CAUSE regression: the old implementation rendered once then called input() --
    nothing updated until Enter. With raw input supported and the very first polled key
    being 'q', the loop must exit after exactly one render/poll cycle WITHOUT ever calling
    the blocking input() builtin."""
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

    record_sample_mock = mock.Mock()
    monkeypatch.setattr(hostnet, "record_sample", record_sample_mock)

    with mock.patch("builtins.input") as input_mock:
        menu.action_view_hostnet(menu.Style(enabled=False))
    input_mock.assert_not_called()
    record_sample_mock.assert_not_called()  # category 15: live view never writes persistent samples


def test_action_view_hostnet_exits_on_0_key_too(tmp_path, monkeypatch):
    monkeypatch.setattr(menu.hostnet_sync, "load_settings",
                         lambda config_dir: {"interface": "eth0"})
    monkeypatch.setattr(menu, "load_display_tz", lambda config_dir: "UTC")
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(menu, "supports_raw_input", lambda: True)
    monkeypatch.setattr(menu, "read_live_interface_counters", lambda iface: (1000, 2000))
    monkeypatch.setattr(menu, "poll_single_key", lambda timeout: "0")
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


def test_action_view_hostnet_ctrl_c_is_caught_gracefully(tmp_path, monkeypatch):
    """Ctrl+C (KeyboardInterrupt) during the live loop must be caught -- no traceback, no
    uncaught exception escaping action_view_hostnet."""
    monkeypatch.setattr(menu.hostnet_sync, "load_settings",
                         lambda config_dir: {"interface": "eth0"})
    monkeypatch.setattr(menu, "load_display_tz", lambda config_dir: "UTC")
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(menu, "supports_raw_input", lambda: True)
    monkeypatch.setattr(menu, "read_live_interface_counters", lambda iface: (1000, 2000))
    monkeypatch.setattr(menu, "clear_screen", lambda: None)

    def raise_interrupt(timeout):
        raise KeyboardInterrupt()
    monkeypatch.setattr(menu, "poll_single_key", raise_interrupt)

    class FakeGuard:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(menu, "_RawTerminalGuard", FakeGuard)

    menu.action_view_hostnet(menu.Style(enabled=False))  # must not raise


def test_raw_terminal_guard_restores_settings_even_on_exception(monkeypatch):
    """Terminal restoration (category 5) must happen even when the body of the `with` block
    raises -- tcsetattr is called from __exit__ regardless of exception."""
    import termios
    fake_fd = 7
    fake_old_settings = ["fake", "settings"]
    monkeypatch.setattr(sys.stdin, "fileno", lambda: fake_fd)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: fake_old_settings)
    monkeypatch.setattr(termios, "tcsetattr", mock.Mock())
    monkeypatch.setattr("tty.setcbreak", lambda fd: None)

    guard = menu._RawTerminalGuard()
    try:
        with guard:
            raise ValueError("boom")
    except ValueError:
        pass
    termios.tcsetattr.assert_called_once_with(fake_fd, termios.TCSADRAIN, fake_old_settings)


def test_supports_raw_input_false_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert menu.supports_raw_input() is False


def test_action_view_hostnet_falls_back_gracefully_when_not_a_tty(tmp_path, monkeypatch):
    """Category 6: graceful fallback (piped stdin, no raw-mode support) -- renders a single
    static snapshot and uses a normal blocking input() to return, never hangs or crashes."""
    monkeypatch.setattr(menu.hostnet_sync, "load_settings",
                         lambda config_dir: {"interface": "eth0"})
    monkeypatch.setattr(menu, "load_display_tz", lambda config_dir: "UTC")
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(menu, "supports_raw_input", lambda: False)
    with mock.patch("builtins.input", return_value="") as input_mock:
        menu.action_view_hostnet(menu.Style(enabled=False))
    input_mock.assert_called_once()


# ---------- 2/3: live speed computation -----------------------------------------------------

def test_compute_live_speed_from_two_sequential_samples():
    prev = (1_000_000, 500_000, 100.0)
    result = menu.compute_live_speed(prev, 1_500_000, 600_000, 101.0)
    assert result == {"rx_bytes_per_sec": 500_000.0, "tx_bytes_per_sec": 100_000.0}


def test_compute_live_speed_none_on_first_tick():
    assert menu.compute_live_speed(None, 1000, 2000, 1.0) is None


def test_compute_live_speed_none_on_counter_decrease_never_negative():
    """A counter that went backwards (interface reset/replacement) must never be reported as
    negative throughput -- see hostnet.py's own discontinuity handling for the persistent
    equivalent of this rule."""
    prev = (5000, 5000, 100.0)
    assert menu.compute_live_speed(prev, 100, 100, 101.0) is None


def test_compute_live_speed_none_on_zero_elapsed_time():
    prev = (1000, 1000, 100.0)
    assert menu.compute_live_speed(prev, 2000, 2000, 100.0) is None


def test_read_live_interface_counters_unavailable_interface_returns_none_tuple(monkeypatch):
    import hostinfo
    monkeypatch.setattr(hostinfo, "_read_interface_counter", lambda iface_dir, stat: "unavailable")
    assert menu.read_live_interface_counters("ghost0") == (None, None)


# ---------- 8: Download/Upload labeling -----------------------------------------------------

def test_live_hostnet_panel_uses_download_upload_labels():
    out = menu.render_live_hostnet_panel(
        "eth0", {"rx_bytes_per_sec": 2_000_000, "tx_bytes_per_sec": 1_000_000}, True,
        {"cumulative_rx_bytes": 10_000_000_000, "cumulative_tx_bytes": 5_000_000_000},
        {"sampled_at": "2026-01-01T00:00:00+00:00", "discontinuity_type": None},
        menu.Style(enabled=False))
    assert "Live Download (RX):" in out
    assert "Live Upload (TX):" in out
    assert "MB/s" in out
    assert "Persistent Download (RX) Total:" in out
    assert "not necessarily any one VPS customer's perspective" in out


def test_live_hostnet_panel_warns_on_unavailable_counters_without_fabricating():
    out = menu.render_live_hostnet_panel(
        "eth0", None, False, None, None, menu.Style(enabled=False))
    assert "WARNING" in out
    assert "unavailable" in out
    assert "MB/s" not in out  # no fabricated speed number when counters are unavailable


# ---------- 9/10/11/12: VPS runtime status (Part 4) -----------------------------------------

def test_map_runtime_status_code_documented_values():
    assert vinfo.map_runtime_status_code(0) == "stopped"
    assert vinfo.map_runtime_status_code(1) == "running"
    assert vinfo.map_runtime_status_code(2) == "suspended"


def test_map_runtime_status_code_unrecognized_value_never_fabricated():
    assert vinfo.map_runtime_status_code(99) == "unavailable"
    assert vinfo.map_runtime_status_code(None) == "unavailable"
    assert vinfo.map_runtime_status_code("not-a-number") == "unavailable"


def test_parse_vs_status_response_extracts_runtime_status_and_network_reason():
    parsed = {"status": {
        "1": {"status": 1, "network_status": False},
        "114": {"status": 1, "network_status": {"id": "2", "reason": "bw", "date": 123}},
    }}
    result = vinfo.parse_vs_status_response(parsed)
    assert result["error"] is None
    assert result["status_by_vpsid"][1] == {"runtime_status": "running", "network_suspended_reason": None}
    assert result["status_by_vpsid"][114] == {"runtime_status": "running", "network_suspended_reason": "bw"}


def test_parse_vs_status_response_missing_status_key_reports_error_not_fabrication():
    result = vinfo.parse_vs_status_response({"something_else": True})
    assert result["status_by_vpsid"] == {}
    assert result["error"] is not None


def test_fetch_vs_status_bulk_makes_exactly_one_call_for_all_vpsids():
    """Category 11: bulk coverage -- never one API request per VPS."""
    calls = []
    def fake_call_vs_api(creds, timeout=20, extra_params=None):
        calls.append(extra_params)
        return {"parsed": {"status": {str(v): {"status": 1, "network_status": False}
                                       for v in range(1, 61)}}}
    result = vinfo.fetch_vs_status_bulk("creds", list(range(1, 61)), call_vs_api=fake_call_vs_api)
    assert len(calls) == 1
    assert len(result["status_by_vpsid"]) == 60


def test_fetch_vs_status_bulk_empty_vpsids_makes_no_call():
    def fake_call_vs_api(creds, timeout=20, extra_params=None):
        raise AssertionError("should not have been called")
    result = vinfo.fetch_vs_status_bulk("creds", [], call_vs_api=fake_call_vs_api)
    assert result == {"status_by_vpsid": {}, "error": None}


def test_fetch_vs_status_bulk_never_sends_a_mutation_shaped_parameter():
    """Category 12: strictly read-only -- only 'vs_status' (and the base act/api/credential
    params handled by call_vs_api itself) is ever added, never start/stop/suspend/etc."""
    captured = {}
    def fake_call_vs_api(creds, timeout=20, extra_params=None):
        captured.update(extra_params or {})
        return {"parsed": {"status": {}}}
    vinfo.fetch_vs_status_bulk("creds", [1, 2, 3], call_vs_api=fake_call_vs_api)
    forbidden = {"bandwidth", "editvps", "theme_edit", "suspend", "unsuspend", "start", "stop", "reboot"}
    assert not (set(captured.keys()) & forbidden)
    assert set(captured.keys()) == {"vs_status"}


def test_fetch_vs_status_bulk_transport_failure_is_non_fatal():
    def failing_call_vs_api(creds, timeout=20, extra_params=None):
        raise ConnectionError("down")
    result = vinfo.fetch_vs_status_bulk("creds", [1], call_vs_api=failing_call_vs_api)
    assert result["status_by_vpsid"] == {}
    assert result["error"] is not None


def test_fetch_vs_inventory_enriches_records_with_runtime_status():
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        if extra_params and "vs_status" in extra_params:
            return {"parsed": {"status": {"1": {"status": 1, "network_status": False},
                                           "2": {"status": 0, "network_status": False}}}}
        page = extra_params.get("page", 1)
        if page == 1:
            return {"parsed": {"vs": {
                "1": {"vpsid": "1", "bandwidth": "100", "used_bandwidth": "10"},
                "2": {"vpsid": "2", "bandwidth": "100", "used_bandwidth": "10"},
            }, "servers": [{"numvps": "2"}]}}
        return {"parsed": {"vs": {}, "servers": [{"numvps": "2"}]}}
    result = vinfo.fetch_vs_inventory("creds", call_vs_api=fake_call_vs_api, sleep_fn=lambda s: None)
    by_id = {r["vpsid"]: r for r in result["records"]}
    assert by_id[1]["runtime_status"] == "running"
    assert by_id[2]["runtime_status"] == "stopped"


def test_fetch_vs_inventory_runtime_status_unavailable_when_enrichment_fails():
    """Category 10/14: a failed status call must never silently become 'running' -- it stays
    the honest default, and the rest of the (already-complete) snapshot is still usable."""
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        if extra_params and "vs_status" in extra_params:
            raise ConnectionError("down")
        page = extra_params.get("page", 1)
        if page == 1:
            return {"parsed": {"vs": {"1": {"vpsid": "1", "bandwidth": "100", "used_bandwidth": "10"}},
                                "servers": [{"numvps": "1"}]}}
        return {"parsed": {"vs": {}, "servers": [{"numvps": "1"}]}}
    result = vinfo.fetch_vs_inventory("creds", call_vs_api=fake_call_vs_api, sleep_fn=lambda s: None)
    assert result["complete"] is True  # enrichment failure doesn't break core inventory completeness
    assert result["records"][0]["runtime_status"] == "unavailable"
    assert result["runtime_status_error"] is not None


# ---------- 13: inventory DB migration compatibility -----------------------------------------

def test_runtime_status_columns_added_to_pre_v1_7_2_schema(tmp_path):
    """Simulates a database created by an older release (no runtime_status/
    network_suspended_reason columns) -- open_db() must add them idempotently, matching the
    existing COLUMN_MIGRATIONS pattern used for every prior column addition."""
    import sqlite3
    db_path = str(tmp_path / "old_inv.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL,
            captured_at TEXT NOT NULL, record_count INTEGER NOT NULL
        );
        CREATE TABLE snapshot_records (
            snapshot_id INTEGER NOT NULL, vpsid INTEGER NOT NULL, hostname TEXT, status TEXT,
            quota_gb TEXT, used_gb TEXT, ram_mb TEXT, cores TEXT, disk_gb TEXT,
            virtualization TEXT, locked TEXT, node_serid TEXT,
            PRIMARY KEY (snapshot_id, vpsid)
        );
        CREATE TABLE known_vps (
            node_id TEXT NOT NULL, vpsid INTEGER NOT NULL, first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL, last_seen_quota_gb TEXT, PRIMARY KEY (node_id, vpsid)
        );
        CREATE TABLE inventory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, vpsid INTEGER NOT NULL,
            event_type TEXT NOT NULL, confidence TEXT NOT NULL, detected_at TEXT NOT NULL,
            old_value TEXT, new_value TEXT, evidence TEXT
        );
    """)
    conn.close()

    conn2 = inv.open_db(db_path)
    cols = [r[1] for r in conn2.execute("PRAGMA table_info(snapshot_records)").fetchall()]
    assert "runtime_status" in cols
    assert "network_suspended_reason" in cols
    conn2.close()
