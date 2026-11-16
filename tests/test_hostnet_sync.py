import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import hostnet_sync as hs


def test_lock_prevents_concurrent_acquisition(tmp_path):
    fh1 = hs.acquire_lock(str(tmp_path))
    try:
        with pytest.raises(hs.HostnetLockHeld):
            hs.acquire_lock(str(tmp_path))
    finally:
        hs.release_lock(fh1)


def test_lock_reacquirable_after_release(tmp_path):
    fh1 = hs.acquire_lock(str(tmp_path))
    hs.release_lock(fh1)
    fh2 = hs.acquire_lock(str(tmp_path))
    hs.release_lock(fh2)


def test_default_settings_when_unset(tmp_path):
    settings = hs.load_settings(str(tmp_path))
    assert settings["interface"] is None
    assert settings["sample_interval_minutes"] == hs.DEFAULT_SAMPLE_INTERVAL_MIN


def test_suggest_interfaces_excludes_virtual_and_uninformative():
    """suggest_interfaces() now delegates to net_topology.select_accounting_candidates(),
    which is tested exhaustively in test_net_topology.py -- this just checks the delegation
    wiring and the real-world 'no topology relationships' case still works end to end."""
    fake_interfaces = [
        {"name": "eth0", "is_virtual": False, "is_uninformative": False, "ipv4": "10.0.0.1",
         "state": "up", "rx_bytes": 1000, "tx_bytes": 500},
        {"name": "viifv1", "is_virtual": True, "is_uninformative": False, "ipv4": "unavailable",
         "state": "up", "rx_bytes": 99999, "tx_bytes": 99999},
        {"name": "eth9", "is_virtual": False, "is_uninformative": True, "ipv4": "unavailable",
         "state": "unavailable", "rx_bytes": "unavailable", "tx_bytes": "unavailable"},
    ]
    result = hs.suggest_interfaces(get_network_interfaces=lambda: fake_interfaces)
    assert [c["name"] for c in result["candidates"]] == ["eth0"]
    assert result["auto_selected"] == "eth0"


def test_suggest_interfaces_sorted_by_traffic_descending():
    fake_interfaces = [
        {"name": "eth0", "is_virtual": False, "is_uninformative": False, "ipv4": "10.0.0.1",
         "state": "up", "rx_bytes": 100, "tx_bytes": 100},
        {"name": "eth1", "is_virtual": False, "is_uninformative": False, "ipv4": "10.0.0.2",
         "state": "up", "rx_bytes": 9000, "tx_bytes": 9000},
    ]
    result = hs.suggest_interfaces(get_network_interfaces=lambda: fake_interfaces)
    assert [c["name"] for c in result["candidates"]] == ["eth1", "eth0"]


def test_save_monitored_interface_requires_confirmation(tmp_path):
    with pytest.raises(ValueError):
        hs.save_monitored_interface(str(tmp_path), "eth0", confirmed=False)


def test_save_monitored_interface_refuses_virtual_interface(tmp_path):
    with pytest.raises(ValueError):
        hs.save_monitored_interface(str(tmp_path), "viifv1234", confirmed=True)


def test_save_and_load_monitored_interface_roundtrip(tmp_path):
    hs.save_monitored_interface(str(tmp_path), "ens1f1", confirmed=True)
    settings = hs.load_settings(str(tmp_path))
    assert settings["interface"] == "ens1f1"


def test_run_hostnet_sample_not_configured_when_no_interface_set(tmp_path):
    result = hs.run_hostnet_sample(str(tmp_path / "state"), str(tmp_path / "config"),
                                    list_interface_names=lambda: ["eth0"])
    assert result["status"] == "NOT_CONFIGURED"


def test_run_hostnet_sample_success_with_injected_counters(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True,
                                 get_network_interfaces=lambda: [])
    result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=lambda: (1000, 500),
                                    list_interface_names=lambda: ["eth0"])
    assert result["status"] == "OK"
    assert result["sample"]["interface"] == "eth0"


def test_run_hostnet_sample_error_on_unreadable_counters(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    def boom():
        raise OSError("no such interface")
    result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=boom,
                                    list_interface_names=lambda: ["eth0"])
    assert result["status"] == "ERROR"


def test_run_hostnet_sample_refuses_when_already_running(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    fh = hs.acquire_lock(state_dir)
    try:
        result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=lambda: (1, 1),
                                        list_interface_names=lambda: ["eth0"])
        assert result["status"] == "ALREADY_RUNNING"
    finally:
        hs.release_lock(fh)


def test_run_hostnet_sample_never_requires_write_approved(tmp_path):
    """Explicit safety requirement: this path must never gate on or touch WRITE_APPROVED."""
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=lambda: (1000, 500),
                                    list_interface_names=lambda: ["eth0"])
    assert result["status"] == "OK"
    assert not os.path.exists(os.path.join(config_dir, "WRITE_APPROVED"))


# ---------- v1.7.0 final: revalidation, rename detection, source-change events ------------

def test_revalidate_interface_not_configured(tmp_path):
    result = hs.revalidate_interface(str(tmp_path / "config"), str(tmp_path / "state"))
    assert result["status"] == "NOT_CONFIGURED"


def test_revalidate_interface_ok_when_still_present(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True, get_network_interfaces=lambda: [])
    result = hs.revalidate_interface(config_dir, state_dir, list_interface_names=lambda: ["eth0", "eth1"])
    assert result["status"] == "OK"


def test_revalidate_interface_detects_rename_via_mac_match(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True,
                                 get_network_interfaces=lambda: [])
    # manually set the persisted MAC since the real sandbox eth0's MAC isn't predictable
    settings = hs.load_settings(config_dir)
    settings["mac_address"] = "aa:bb:cc:dd:ee:ff"
    import json
    (__import__("pathlib").Path(config_dir) / "hostnet-settings.json").write_text(json.dumps(settings))

    result = hs.revalidate_interface(
        config_dir, state_dir, list_interface_names=lambda: ["eth7"],
        get_mac=lambda name: "aa:bb:cc:dd:ee:ff" if name == "eth7" else "unavailable")
    assert result["status"] == "RENAMED"
    assert result["new_interface"] == "eth7"
    new_settings = hs.load_settings(config_dir)
    assert new_settings["interface"] == "eth7"


def test_revalidate_interface_missing_when_no_mac_match_clears_config(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True, get_network_interfaces=lambda: [])
    settings = hs.load_settings(config_dir)
    settings["mac_address"] = "aa:bb:cc:dd:ee:ff"
    import json
    (__import__("pathlib").Path(config_dir) / "hostnet-settings.json").write_text(json.dumps(settings))

    result = hs.revalidate_interface(
        config_dir, state_dir, list_interface_names=lambda: ["eth7"],
        get_mac=lambda name: "11:22:33:44:55:66")  # different MAC -- not a match
    assert result["status"] == "MISSING"
    new_settings = hs.load_settings(config_dir)
    assert new_settings["interface"] is None


def test_revalidate_interface_never_merges_history_across_rename(tmp_path):
    """The rename path updates the ACTIVE pointer, but hostnet.py's cumulative table is
    keyed by exact interface name -- the old name's history row is untouched/separate."""
    import hostnet
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True, get_network_interfaces=lambda: [])
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    hostnet.record_sample(conn, "eth0", 1000, 500, boot_id="b", sampled_at="2026-01-01T00:00:00+00:00")

    settings = hs.load_settings(config_dir)
    settings["mac_address"] = "aa:bb:cc:dd:ee:ff"
    import json
    (__import__("pathlib").Path(config_dir) / "hostnet-settings.json").write_text(json.dumps(settings))
    hs.revalidate_interface(config_dir, state_dir, list_interface_names=lambda: ["eth7"],
                             get_mac=lambda name: "aa:bb:cc:dd:ee:ff")

    old_cumulative = hostnet.get_cumulative(conn, "eth0")
    new_cumulative = hostnet.get_cumulative(conn, "eth7")
    assert old_cumulative["cumulative_rx_bytes"] == 0  # untouched, never merged
    assert new_cumulative is None  # eth7 has no samples of its own yet -- a fresh segment


def test_save_monitored_interface_records_source_event(tmp_path):
    import hostnet
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True, state_dir=state_dir)
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    events = hostnet.get_source_events(conn)
    assert events[0]["event_type"] == hostnet.SOURCE_EVENT_CONFIGURED
    assert events[0]["new_interface"] == "eth0"
