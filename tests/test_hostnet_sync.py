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
    fake_interfaces = [
        {"name": "eth0", "is_virtual": False, "is_uninformative": False, "rx_bytes": 1000, "tx_bytes": 500},
        {"name": "viifv1", "is_virtual": True, "is_uninformative": False, "rx_bytes": 99999, "tx_bytes": 99999},
        {"name": "eth9", "is_virtual": False, "is_uninformative": True, "rx_bytes": "unavailable", "tx_bytes": "unavailable"},
    ]
    result = hs.suggest_interfaces(get_network_interfaces=lambda: fake_interfaces)
    assert [i["name"] for i in result] == ["eth0"]


def test_suggest_interfaces_sorted_by_traffic_descending():
    fake_interfaces = [
        {"name": "eth0", "is_virtual": False, "is_uninformative": False, "rx_bytes": 100, "tx_bytes": 100},
        {"name": "eth1", "is_virtual": False, "is_uninformative": False, "rx_bytes": 9000, "tx_bytes": 9000},
    ]
    result = hs.suggest_interfaces(get_network_interfaces=lambda: fake_interfaces)
    assert [i["name"] for i in result] == ["eth1", "eth0"]


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
    result = hs.run_hostnet_sample(str(tmp_path / "state"), str(tmp_path / "config"))
    assert result["status"] == "NOT_CONFIGURED"


def test_run_hostnet_sample_success_with_injected_counters(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=lambda: (1000, 500))
    assert result["status"] == "OK"
    assert result["sample"]["interface"] == "eth0"


def test_run_hostnet_sample_error_on_unreadable_counters(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    def boom():
        raise OSError("no such interface")
    result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=boom)
    assert result["status"] == "ERROR"


def test_run_hostnet_sample_refuses_when_already_running(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    fh = hs.acquire_lock(state_dir)
    try:
        result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=lambda: (1, 1))
        assert result["status"] == "ALREADY_RUNNING"
    finally:
        hs.release_lock(fh)


def test_run_hostnet_sample_never_requires_write_approved(tmp_path):
    """Explicit safety requirement: this path must never gate on or touch WRITE_APPROVED."""
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    hs.save_monitored_interface(config_dir, "eth0", confirmed=True)
    result = hs.run_hostnet_sample(state_dir, config_dir, read_counters_fn=lambda: (1000, 500))
    assert result["status"] == "OK"
    assert not os.path.exists(os.path.join(config_dir, "WRITE_APPROVED"))
