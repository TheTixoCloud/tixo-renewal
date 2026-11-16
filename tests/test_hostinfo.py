import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import hostinfo


def test_get_os_info_returns_hostname_and_kernel():
    info = hostinfo.get_os_info()
    assert info["hostname"]
    assert info["kernel"]
    assert info["architecture"]


def test_get_cpu_info_reports_logical_cpus():
    info = hostinfo.get_cpu_info()
    assert isinstance(info["logical_cpus"], int)
    assert info["logical_cpus"] > 0


def test_get_memory_info_reports_total_mb_as_int_on_linux():
    info = hostinfo.get_memory_info()
    assert isinstance(info["total_mb"], int)
    assert info["total_mb"] > 0


def test_get_disk_info_reports_root_mount():
    results = hostinfo.get_disk_info(["/"])
    assert len(results) == 1
    assert results[0]["mount_point"] == "/"
    assert isinstance(results[0]["total_gb"], float)


def test_get_disk_info_nonexistent_mount_reports_unavailable_not_zero():
    results = hostinfo.get_disk_info(["/no/such/mount/point/xyz"])
    assert results[0]["total_gb"] == hostinfo.UNAVAILABLE


def test_get_uptime_and_load_returns_numeric_load():
    info = hostinfo.get_uptime_and_load()
    assert isinstance(info["uptime_seconds"], float)
    assert "1m" in info["load_average"]


def test_get_temperature_info_never_raises_and_distinguishes_unavailable():
    info = hostinfo.get_temperature_info()
    assert info["available"] in (True, False)
    if not info["available"]:
        assert info["reason"] in (hostinfo.UNAVAILABLE, hostinfo.UNSUPPORTED, hostinfo.PERMISSION_DENIED)


def test_get_network_interfaces_never_raises():
    interfaces = hostinfo.get_network_interfaces()
    assert isinstance(interfaces, list)
    assert all("lo" != i["name"] for i in interfaces)


def test_collect_host_info_assembles_everything_without_raising():
    info = hostinfo.collect_host_info()
    assert set(info.keys()) == {
        "os", "cpu", "memory", "disks", "uptime", "usage", "temperature",
        "network_interfaces", "host_timezone",
    }


def test_read_text_distinguishes_missing_from_permission_denied(tmp_path, monkeypatch):
    missing = str(tmp_path / "does-not-exist")
    assert hostinfo._read_text(missing) is None

    restricted = tmp_path / "restricted"
    restricted.write_text("secret")
    os.chmod(restricted, 0o000)
    try:
        if os.geteuid() == 0:
            # root bypasses permission bits -- skip the assertion, can't simulate denial.
            return
        assert hostinfo._read_text(str(restricted)) == hostinfo.PERMISSION_DENIED
    finally:
        os.chmod(restricted, 0o644)
