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


# ---------- v1.5.0: distinguish physical/management interfaces from virtual ones ----------

def test_is_virtual_interface_recognizes_common_virtualization_prefixes():
    assert hostinfo.is_virtual_interface("vnet12") is True
    assert hostinfo.is_virtual_interface("tap0") is True
    assert hostinfo.is_virtual_interface("virbr0") is True
    assert hostinfo.is_virtual_interface("veth1234") is True
    assert hostinfo.is_virtual_interface("docker0") is True


def test_is_virtual_interface_false_for_physical_looking_names():
    assert hostinfo.is_virtual_interface("eth0") is False
    assert hostinfo.is_virtual_interface("enp3s0") is False
    assert hostinfo.is_virtual_interface("eno1") is False


def test_get_network_interfaces_tags_is_virtual_field():
    interfaces = hostinfo.get_network_interfaces()
    assert all("is_virtual" in i for i in interfaces)


# ---------- v1.7.0: viifv* filtering (real production bug) + uninformative-interface filter

def test_is_virtual_interface_recognizes_viifv_prefix():
    """Regression test: the operator observed real viifvXXXX interfaces still showing on the
    'default' host info screen on ir1 despite the virtual-interface-hiding feature -- viifv
    (Virtualizor's own per-VM interface naming convention) was missing from the prefix list."""
    assert hostinfo.is_virtual_interface("viifv1234") is True
    assert hostinfo.is_virtual_interface("viifv0") is True


# ---------- classify_interface_category / filter_interfaces (advanced-view search/filter) -----

def test_classify_interface_category_physical():
    assert hostinfo.classify_interface_category("ens1f1") == "physical"
    assert hostinfo.classify_interface_category("eth0") == "physical"


def test_classify_interface_category_bridge():
    assert hostinfo.classify_interface_category("viifbr0") == "bridge"


def test_classify_interface_category_virtual_takes_priority_over_bridge_substring():
    # "virbr0" matches the virtual-prefix list AND contains "br" -- virtual wins.
    assert hostinfo.classify_interface_category("virbr0") == "virtual"
    assert hostinfo.classify_interface_category("viifv1001") == "virtual"


def test_filter_interfaces_by_category():
    interfaces = [{"name": "ens1f1"}, {"name": "viifbr0"}, {"name": "viifv1001"},
                  {"name": "viifv1002"}]
    assert [i["name"] for i in hostinfo.filter_interfaces(interfaces, category="physical")] \
        == ["ens1f1"]
    assert [i["name"] for i in hostinfo.filter_interfaces(interfaces, category="bridge")] \
        == ["viifbr0"]
    assert [i["name"] for i in hostinfo.filter_interfaces(interfaces, category="virtual")] \
        == ["viifv1001", "viifv1002"]


def test_filter_interfaces_category_all_is_a_no_op():
    interfaces = [{"name": "ens1f1"}, {"name": "viifbr0"}]
    assert hostinfo.filter_interfaces(interfaces, category="all") == interfaces
    assert hostinfo.filter_interfaces(interfaces, category=None) == interfaces


def test_filter_interfaces_by_search_term_case_insensitive():
    interfaces = [{"name": "ens1f1"}, {"name": "viifbr0"}, {"name": "viifv1001"}]
    assert [i["name"] for i in hostinfo.filter_interfaces(interfaces, search="VIIF")] \
        == ["viifbr0", "viifv1001"]
    assert [i["name"] for i in hostinfo.filter_interfaces(interfaces, search="ens")] \
        == ["ens1f1"]


def test_filter_interfaces_combines_category_and_search():
    interfaces = [{"name": "viifbr0"}, {"name": "viifbr1"}, {"name": "viifv1001"}]
    result = hostinfo.filter_interfaces(interfaces, category="bridge", search="1")
    assert [i["name"] for i in result] == ["viifbr1"]


def test_get_network_interfaces_includes_rx_tx_bytes():
    interfaces = hostinfo.get_network_interfaces()
    assert all("rx_bytes" in i and "tx_bytes" in i for i in interfaces)


def test_get_network_interfaces_flags_uninformative_entries():
    interfaces = hostinfo.get_network_interfaces()
    assert all("is_uninformative" in i for i in interfaces)


def test_uninformative_flag_true_only_when_truly_uninformative_and_not_virtual():
    # Simulate the shape directly since we can't control real /sys/class/net content.
    def classify(name, state, ipv4, rx):
        is_virtual = hostinfo.is_virtual_interface(name)
        return (not is_virtual and state == hostinfo.UNAVAILABLE and not ipv4
                and rx == hostinfo.UNAVAILABLE)
    assert classify("eth9", hostinfo.UNAVAILABLE, None, hostinfo.UNAVAILABLE) is True
    assert classify("viifv1", hostinfo.UNAVAILABLE, None, hostinfo.UNAVAILABLE) is False
    assert classify("eth0", "up", "10.0.0.1", 12345) is False


# ---------- v1.9.1: _is_uninformative() -- real production bug found during v1.9.0 upgrade ----

def test_is_uninformative_true_for_down_unused_physical_port():
    """Regression test: ir1 production validation found 4 unused physical NIC bays
    (eno1-4, all state='down', zero counters, no IP) were NOT being classified as
    uninformative, because the old check only matched state == UNAVAILABLE (state
    unreadable), never the real, readable value "down"."""
    assert hostinfo._is_uninformative("down", None, 0, 0, False) is True


def test_is_uninformative_false_for_up_interface_with_real_traffic():
    assert hostinfo._is_uninformative("up", None, 248963369375193,
                                       253692770900704, False) is False


def test_is_uninformative_false_for_down_interface_that_still_has_an_ip():
    # A down interface with a statically-assigned IP is still worth showing.
    assert hostinfo._is_uninformative("down", "10.0.0.5", 0, 0, False) is False


def test_is_uninformative_false_for_down_interface_with_nonzero_counters():
    # Historical traffic (even if currently down) is still informative.
    assert hostinfo._is_uninformative("down", None, 500, 0, False) is False


def test_is_uninformative_false_for_virtual_interface_regardless_of_state():
    # Virtual interfaces are handled by is_virtual, never by is_uninformative.
    assert hostinfo._is_uninformative("down", None, 0, 0, True) is False


def test_is_uninformative_true_for_truly_unreadable_state():
    assert hostinfo._is_uninformative(hostinfo.UNAVAILABLE, None, 0, 0, False) is True
