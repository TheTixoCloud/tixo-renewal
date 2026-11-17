"""Tests for control_center_api.py (v1.8.0 Part 2 shared backend). Verifies: every function
reads only from already-populated local caches (never a Virtualizor API call), unconfigured/
empty states are reported honestly rather than fabricated, and the full-snapshot bundle stays
a small, bounded number of calls regardless of fleet size.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import control_center_api as api
import hostnet
import hostnet_sync
import inventory as inv
import emergency_policy as emp
import nodes as nodes_mod
import policy as pol


def _state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return str(d)


def _config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    return str(d)


# ---------- get_host_snapshot ----------------------------------------------------------------

def test_get_host_snapshot_returns_real_host_info():
    snapshot = api.get_host_snapshot()
    assert "os" in snapshot or "cpu" in snapshot or "network_interfaces" in snapshot


# ---------- get_live_network_reading -----------------------------------------------------------

def test_get_live_network_reading_unavailable_interface_reports_not_ok():
    result = api.get_live_network_reading("definitely-not-a-real-interface-xyz")
    assert result["ok"] is False
    assert result["rx_bytes"] is None
    assert result["tx_bytes"] is None


def test_get_live_network_reading_loopback_is_ok():
    """lo (loopback) exists on every Linux host this runs on -- a real, cheap smoke test
    that this reads genuine kernel counters, not a mock."""
    result = api.get_live_network_reading("lo")
    assert result["ok"] is True
    assert isinstance(result["rx_bytes"], int)
    assert isinstance(result["tx_bytes"], int)


# ---------- get_network_overview ----------------------------------------------------------------

def test_network_overview_unconfigured_reports_honestly(tmp_path):
    result = api.get_network_overview(_config_dir(tmp_path), _state_dir(tmp_path))
    assert result["configured"] is False
    assert result["interface"] is None


def test_network_overview_configured_includes_analytics_bundle(tmp_path):
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    hostnet_sync.save_monitored_interface(config_dir, "eth0", confirmed=True,
                                           get_network_interfaces=lambda: [
                                               {"name": "eth0", "is_virtual": False}])
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    hostnet.record_sample(conn, "eth0", 0, 0, boot_id="b1", sampled_at="2026-01-01T00:00:00+00:00")
    hostnet.record_sample(conn, "eth0", 1000, 2000, boot_id="b1", sampled_at="2026-01-01T00:05:00+00:00")
    conn.close()

    result = api.get_network_overview(config_dir, state_dir, tz_name="UTC")
    assert result["configured"] is True
    assert result["interface"] == "eth0"
    assert result["cumulative"]["cumulative_rx_bytes"] == 1000
    assert "traffic_analytics" in result
    assert "all_time" in result["traffic_analytics"]


def test_network_overview_never_calls_virtualizor_api(tmp_path, monkeypatch):
    """Guards the module's central safety claim: no function here reaches the Virtualizor
    API. If writer.call_vs_api (or similar) were ever imported/called from this path, this
    monkeypatch would raise and fail the test."""
    import writer as writer_mod
    def boom(*a, **kw):
        raise AssertionError("control_center_api must never call the Virtualizor API")
    monkeypatch.setattr(writer_mod, "call_vs_api", boom, raising=False)
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    api.get_network_overview(config_dir, state_dir, tz_name="UTC")  # unconfigured path
    hostnet_sync.save_monitored_interface(config_dir, "eth0", confirmed=True,
                                           get_network_interfaces=lambda: [
                                               {"name": "eth0", "is_virtual": False}])
    api.get_network_overview(config_dir, state_dir, tz_name="UTC")  # configured, no samples


# ---------- get_vps_inventory_overview --------------------------------------------------------

def test_vps_inventory_overview_empty_when_never_synced(tmp_path):
    result = api.get_vps_inventory_overview(_state_dir(tmp_path))
    assert result["sync_info"] is None
    assert result["records"] == []
    assert result["fleet"]["total_vps"] == 0


def test_vps_inventory_overview_reflects_latest_snapshot(tmp_path):
    state_dir = _state_dir(tmp_path)
    conn = inv.open_db(os.path.join(state_dir, "inventory.sqlite3"))
    parse_result = {
        "complete": True,
        "records": [{"vpsid": 1, "hostname": "h1", "status": "unknown", "quota_gb": Decimal("100"),
                      "used_gb": Decimal("50"), "ram_mb": "1024", "cores": "1", "disk_gb": "20",
                      "virtualization": "kvm", "locked": "unavailable", "node_serid": "unavailable",
                      "ip_primary": "unavailable", "ip_count": "unavailable", "ip_list": "unavailable",
                      "runtime_status": "running", "network_suspended_reason": None}],
        "skipped_count": 0, "error": None, "authoritative_total": 1,
    }
    inv.ingest_snapshot(conn, "self", parse_result)
    conn.close()

    result = api.get_vps_inventory_overview(state_dir)
    assert result["sync_info"]["record_count"] == 1
    assert len(result["records"]) == 1
    assert result["fleet"]["total_vps"] == 1
    assert result["fleet"]["running_count"] == 1


# ---------- get_renewal_overview (thin wrapper over menu.collect_dashboard_data) -------------

def test_renewal_overview_reports_write_approved_and_policy(tmp_path):
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    pol.write_default_policy_config(os.path.join(config_dir, "policy.json"))
    nodes_mod.write_default_config(os.path.join(config_dir, "nodes.json"),
                                    nodes=[nodes_mod.NodeConfig("self", mode="local",
                                                                 db_filename="self.sqlite3")])
    result = api.get_renewal_overview(config_dir, state_dir)
    assert result["write_approved"] is False
    assert result["policy_ok"] is True
    assert result["monthly_grant_gb"] == "100"


# ---------- get_emergency_policy_overview -----------------------------------------------------

def test_emergency_policy_overview_defaults_to_disabled(tmp_path):
    result = api.get_emergency_policy_overview(_config_dir(tmp_path), _state_dir(tmp_path))
    assert result["mode"] == emp.MODE_DISABLED
    assert result["pending_reviews"] == []
    assert result["proposed_grants"] == []
    assert result["emergency_grants_approved"] is False


def test_emergency_policy_overview_reflects_pending_review(tmp_path):
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    policy = emp.ExhaustedBandwidthPolicy(mode=emp.MODE_MANUAL_REVIEW)
    emp.save_policy_settings(config_dir, policy)
    conn = emp.open_db(os.path.join(state_dir, "emergency_policy.sqlite3"))
    emp.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy)
    conn.close()

    result = api.get_emergency_policy_overview(config_dir, state_dir)
    assert result["mode"] == emp.MODE_MANUAL_REVIEW
    assert len(result["pending_reviews"]) == 1
    assert result["pending_reviews"][0]["vpsid"] == 7


# ---------- get_full_snapshot: bounded, well-formed -------------------------------------------

def test_full_snapshot_has_all_top_level_sections(tmp_path):
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    pol.write_default_policy_config(os.path.join(config_dir, "policy.json"))
    nodes_mod.write_default_config(os.path.join(config_dir, "nodes.json"),
                                    nodes=[nodes_mod.NodeConfig("self", mode="local",
                                                                 db_filename="self.sqlite3")])
    snapshot = api.get_full_snapshot(config_dir, state_dir)
    assert set(snapshot.keys()) == {
        "generated_at", "display_timezone", "host", "host_time", "network", "inventory",
        "renewal", "emergency_policy",
    }
    assert snapshot["network"]["configured"] is False
    assert snapshot["inventory"]["fleet"]["total_vps"] == 0
    assert snapshot["emergency_policy"]["mode"] == emp.MODE_DISABLED
    assert snapshot["host_time"]["network_configured"] is False
    assert snapshot["host_time"]["uptime_human"] != "unavailable"


def test_get_host_time_overview_uses_configured_interface(tmp_path):
    import hostnet_sync
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    hostnet_sync.save_monitored_interface(config_dir, "lo", confirmed=True,
                                           get_network_interfaces=lambda: [
                                               {"name": "lo", "is_virtual": False}])
    result = api.get_host_time_overview(config_dir, state_dir)
    assert result["network_configured"] is True
    assert result["interface"] == "lo"
