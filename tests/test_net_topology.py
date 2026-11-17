import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import net_topology as nt


def iface(name, is_virtual=False, is_uninformative=False, ipv4="unavailable",
          state="up", rx_bytes=1000, tx_bytes=1000):
    return {"name": name, "is_virtual": is_virtual, "is_uninformative": is_uninformative,
            "ipv4": ipv4, "state": state, "rx_bytes": rx_bytes, "tx_bytes": tx_bytes}


# ---------- single physical NIC, various naming schemes -----------------------------------

def test_single_physical_nic_auto_selected(monkeypatch):
    monkeypatch.setattr(nt, "build_topology", lambda names: {
        n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None} for n in names})
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"ens1f1"}, "ipv6": set()})
    result = nt.select_accounting_candidates([iface("ens1f1", ipv4="10.0.0.1")])
    assert result["auto_selected"] == "ens1f1"


def test_different_predictable_interface_names_all_work(monkeypatch):
    """eno0, enp3s0, eth0 -- the module must never hardcode a specific name pattern."""
    for name in ("eno0", "enp3s0", "eth0", "ens1f1", "p1p1"):
        monkeypatch.setattr(nt, "build_topology", lambda names, n=name: {
            n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None}})
        monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": set(), "ipv6": set()})
        result = nt.select_accounting_candidates([iface(name)])
        assert result["auto_selected"] == name


# ---------- bridge over a physical NIC -----------------------------------------------------

def test_bridge_with_one_physical_member_selects_the_member_not_the_bridge(monkeypatch):
    """Real-world finding (verified against ir1's actual topology, read-only, by comparing
    counter values): a Linux bridge DEVICE's own rx/tx counters only count traffic locally
    delivered to the bridge's own address -- NOT the full switched/forwarded traffic. The
    physical member sees everything on the wire and is the correct accounting source; the
    bridge device itself must be excluded here, the REVERSE of the bond-member case."""
    def fake_topology(names):
        return {
            "eth0": {"name": "eth0", "role": nt.ROLE_BRIDGE_MEMBER, "master": "br0", "parent": None},
            "br0": {"name": "br0", "role": nt.ROLE_BRIDGE, "master": None, "parent": None},
        }
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"br0"}, "ipv6": set()})
    monkeypatch.setattr(nt, "read_bridge_members", lambda name: ["eth0"] if name == "br0" else [])
    monkeypatch.setattr(nt, "has_hardware_device", lambda name: name == "eth0")
    result = nt.select_accounting_candidates([iface("eth0", state="up", rx_bytes=5000, tx_bytes=5000),
                                                iface("br0", ipv4="10.0.0.1", state="up", rx_bytes=10, tx_bytes=10)])
    assert result["auto_selected"] == "eth0"
    assert any(e["name"] == "br0" and "bridge device" in e["exclusion_reason"] for e in result["excluded"])


def test_bridge_with_multiple_physical_members_stays_ambiguous(monkeypatch):
    """Two physical NICs directly bridged (no bonding) for redundancy -- genuinely
    ambiguous, never auto-resolved."""
    def fake_topology(names):
        return {
            "eth0": {"name": "eth0", "role": nt.ROLE_BRIDGE_MEMBER, "master": "br0", "parent": None},
            "eth1": {"name": "eth1", "role": nt.ROLE_BRIDGE_MEMBER, "master": "br0", "parent": None},
            "br0": {"name": "br0", "role": nt.ROLE_BRIDGE, "master": None, "parent": None},
        }
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"br0"}, "ipv6": set()})
    monkeypatch.setattr(nt, "read_bridge_members", lambda name: ["eth0", "eth1"] if name == "br0" else [])
    monkeypatch.setattr(nt, "has_hardware_device", lambda name: name in ("eth0", "eth1"))
    result = nt.select_accounting_candidates([
        iface("eth0", state="up"), iface("eth1", state="up"), iface("br0", ipv4="10.0.0.1", state="up")])
    assert result["auto_selected"] is None
    assert len(result["candidates"]) == 3


def test_bridge_with_zero_physical_members_stays_ambiguous(monkeypatch):
    """A purely virtual/internal bridge (no real physical uplink member) -- the exclusion
    rule doesn't apply; the bridge itself remains a candidate rather than being blindly
    treated as correct or incorrect."""
    def fake_topology(names):
        return {"br0": {"name": "br0", "role": nt.ROLE_BRIDGE, "master": None, "parent": None}}
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": set(), "ipv6": set()})
    monkeypatch.setattr(nt, "read_bridge_members", lambda name: [])
    monkeypatch.setattr(nt, "has_hardware_device", lambda name: False)
    result = nt.select_accounting_candidates([iface("br0", ipv4="10.0.0.1", state="up")])
    assert result["auto_selected"] == "br0"  # the only candidate at all, nothing to disambiguate


def test_down_interface_excluded_even_if_otherwise_a_plausible_candidate(monkeypatch):
    """Portable, name-agnostic signal: an administratively/physically down interface cannot
    be the live accounting source, regardless of what it's named."""
    def fake_topology(names):
        return {n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None} for n in names}
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"eth0"}, "ipv6": set()})
    result = nt.select_accounting_candidates([
        iface("eth0", state="up", ipv4="10.0.0.1"),
        iface("eth1", state="down"), iface("eth2", state="down"), iface("eth3", state="down"),
    ])
    assert result["auto_selected"] == "eth0"
    down_names = {e["name"] for e in result["excluded"]}
    assert down_names == {"eth1", "eth2", "eth3"}


def test_reproduces_real_ir1_topology_end_to_end(monkeypatch):
    """Full reproduction of ir1's actual observed topology: ens1f1 (physical, up, huge
    traffic) is a bridge member of viifbr0 (holds the real IP + default routes, but tiny
    counters); several dead physical NICs (down, zero traffic) and many viifv* VM taps are
    also bridge members. Must resolve to ens1f1 alone, automatically, with no manual
    per-host configuration."""
    def fake_topology(names):
        topo = {"viifbr0": {"name": "viifbr0", "role": nt.ROLE_BRIDGE, "master": None, "parent": None}}
        for n in names:
            if n == "viifbr0":
                continue
            if n == "ens1f1" or n.startswith("viifv"):
                topo[n] = {"name": n, "role": nt.ROLE_BRIDGE_MEMBER, "master": "viifbr0", "parent": None}
            else:
                topo[n] = {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None}
        return topo
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces",
                         lambda: {"ipv4": {"viifbr0"}, "ipv6": {"viifbr0"}})
    monkeypatch.setattr(nt, "read_bridge_members",
                         lambda name: ["ens1f1", "viifv1001", "viifv1002"] if name == "viifbr0" else [])
    monkeypatch.setattr(nt, "has_hardware_device", lambda name: name in ("ens1f1", "eno1", "eno2", "ens1f0"))

    interfaces = [
        iface("eno1", state="down", rx_bytes=0, tx_bytes=0),
        iface("eno2", state="down", rx_bytes=0, tx_bytes=0),
        iface("ens1f0", state="down", rx_bytes=0, tx_bytes=0),
        iface("ens1f1", state="up", rx_bytes=217_000_000_000_000, tx_bytes=222_000_000_000_000),
        iface("viifbr0", state="up", ipv4="87.107.168.129", rx_bytes=97_000_000_000, tx_bytes=6_000_000_000_000),
        iface("viifv1001", is_virtual=True, state="unknown", rx_bytes=2_000_000_000, tx_bytes=1_000_000_000),
        iface("viifv1002", is_virtual=True, state="unknown", rx_bytes=5_000_000_000, tx_bytes=3_000_000_000),
    ]
    result = nt.select_accounting_candidates(interfaces)
    assert result["auto_selected"] == "ens1f1"
    excluded_names = {e["name"] for e in result["excluded"]}
    assert excluded_names == {"eno1", "eno2", "ens1f0", "viifbr0", "viifv1001", "viifv1002"}


# ---------- bond with multiple physical members --------------------------------------------

def test_bond_members_excluded_bond_master_auto_selected(monkeypatch):
    def fake_topology(names):
        return {
            "eth0": {"name": "eth0", "role": nt.ROLE_BOND_MEMBER, "master": "bond0", "parent": None},
            "eth1": {"name": "eth1", "role": nt.ROLE_BOND_MEMBER, "master": "bond0", "parent": None},
            "bond0": {"name": "bond0", "role": nt.ROLE_BOND_MASTER, "master": None, "parent": None},
        }
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"bond0"}, "ipv6": set()})
    result = nt.select_accounting_candidates([iface("eth0"), iface("eth1"), iface("bond0", ipv4="10.0.0.5")])
    assert result["auto_selected"] == "bond0"
    excluded_names = {e["name"] for e in result["excluded"]}
    assert excluded_names == {"eth0", "eth1"}


# ---------- VLAN over a physical NIC: genuine ambiguity, never auto-collapsed -------------

def test_vlan_and_parent_both_remain_candidates_ambiguous(monkeypatch):
    def fake_topology(names):
        return {
            "eth0": {"name": "eth0", "role": nt.ROLE_PHYSICAL, "master": None, "parent": None},
            "eth0.100": {"name": "eth0.100", "role": nt.ROLE_VLAN, "master": None, "parent": "eth0"},
        }
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": set(), "ipv6": set()})
    result = nt.select_accounting_candidates([iface("eth0", ipv4="10.0.0.1"), iface("eth0.100", ipv4="10.0.1.1")])
    assert result["auto_selected"] is None
    assert len(result["candidates"]) == 2
    names = {c["name"] for c in result["candidates"]}
    assert names == {"eth0", "eth0.100"}
    vlan_candidate = next(c for c in result["candidates"] if c["name"] == "eth0.100")
    assert vlan_candidate["parent"] == "eth0"


# ---------- multiple default routes --------------------------------------------------------

def test_multiple_default_routes_does_not_force_a_single_choice(monkeypatch):
    def fake_topology(names):
        return {n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None} for n in names}
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces",
                         lambda: {"ipv4": {"eth0", "eth1"}, "ipv6": set()})
    result = nt.select_accounting_candidates([iface("eth0", ipv4="10.0.0.1"), iface("eth1", ipv4="10.0.0.2")])
    assert result["auto_selected"] is None
    assert len(result["candidates"]) == 2
    assert all(c["is_default_route_v4"] for c in result["candidates"])


# ---------- IPv6-only networking ------------------------------------------------------------

def test_ipv6_only_default_route_detected_as_evidence(monkeypatch):
    def fake_topology(names):
        return {n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None} for n in names}
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": set(), "ipv6": {"eth0"}})
    result = nt.select_accounting_candidates([iface("eth0", ipv4="unavailable")])
    assert result["auto_selected"] == "eth0"
    assert result["candidates"][0]["is_default_route_v6"] is True


# ---------- separate management and VPS traffic networks (two real NICs, no default route) -

def test_separate_management_and_vps_networks_both_candidates_no_default_route(monkeypatch):
    def fake_topology(names):
        return {n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None} for n in names}
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    # eth0 = management (has the default route), eth1 = dedicated VPS uplink (no default route
    # but carries real traffic) -- both are legitimate candidates, never auto-resolved.
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"eth0"}, "ipv6": set()})
    result = nt.select_accounting_candidates([
        iface("eth0", ipv4="10.0.0.1", rx_bytes=100, tx_bytes=100),
        iface("eth1", ipv4="10.0.1.1", rx_bytes=999999, tx_bytes=999999),
    ])
    assert result["auto_selected"] is None
    assert len(result["candidates"]) == 2
    # most-active-first ordering, but NOT auto-selected just because it's busier
    assert result["candidates"][0]["name"] == "eth1"


# ---------- double-count prevention: virtual + uninformative always excluded --------------

def test_virtual_and_uninformative_interfaces_always_excluded(monkeypatch):
    def fake_topology(names):
        return {n: {"name": n, "role": nt.ROLE_PHYSICAL, "master": None, "parent": None} for n in names}
    monkeypatch.setattr(nt, "build_topology", fake_topology)
    monkeypatch.setattr(nt, "get_default_route_interfaces", lambda: {"ipv4": {"eth0"}, "ipv6": set()})
    result = nt.select_accounting_candidates([
        iface("eth0", ipv4="10.0.0.1"),
        iface("viifv1234", is_virtual=True),
        iface("eth9", is_uninformative=True, ipv4="unavailable", rx_bytes="unavailable", tx_bytes="unavailable"),
    ])
    assert result["auto_selected"] == "eth0"
    excluded_names = {e["name"] for e in result["excluded"]}
    assert excluded_names == {"viifv1234", "eth9"}


# ---------- classify_interface direct tests (role detection) -------------------------------

def test_classify_interface_vlan_detected_via_proc_vlan_config(monkeypatch):
    result = nt.classify_interface("eth0.100", vlan_parents={"eth0.100": "eth0"})
    assert result["role"] == nt.ROLE_VLAN
    assert result["parent"] == "eth0"


def test_classify_interface_bond_member_role(monkeypatch, tmp_path):
    monkeypatch.setattr(nt, "get_interface_master", lambda name: "bond0" if name == "eth0" else None)
    monkeypatch.setattr(nt, "is_bond_master", lambda name: name == "bond0")
    monkeypatch.setattr(nt, "is_bridge", lambda name: False)
    result = nt.classify_interface("eth0", vlan_parents={})
    assert result["role"] == nt.ROLE_BOND_MEMBER
    assert result["master"] == "bond0"


def test_classify_interface_bridge_member_role(monkeypatch):
    monkeypatch.setattr(nt, "get_interface_master", lambda name: "br0" if name == "eth0" else None)
    monkeypatch.setattr(nt, "is_bond_master", lambda name: False)
    monkeypatch.setattr(nt, "is_bridge", lambda name: name == "br0")
    result = nt.classify_interface("eth0", vlan_parents={})
    assert result["role"] == nt.ROLE_BRIDGE_MEMBER
    assert result["master"] == "br0"


def test_read_vlan_parents_returns_empty_when_no_vlan_module(tmp_path, monkeypatch):
    import pathlib
    monkeypatch.setattr(nt, "Path", lambda p: pathlib.Path("/no/such/path/at/all"))
    assert nt.read_vlan_parents() == {}


def test_get_default_route_interfaces_never_raises_when_proc_missing(monkeypatch):
    import pathlib
    real_path = pathlib.Path
    monkeypatch.setattr(nt, "Path", lambda p: real_path("/no/such/proc/route/file"))
    result = nt.get_default_route_interfaces()
    assert result == {"ipv4": set(), "ipv6": set()}


def test_recommendation_reason_mentions_default_route():
    reason = nt.recommendation_reason({"role": "physical", "is_default_route_v4": True,
                                        "is_default_route_v6": False, "parent": None, "ipv4": "10.0.0.1"})
    assert "default route" in reason


# ---------- regression: get_interface_master() non-strict resolve() bug -------------------

def test_get_interface_master_none_when_no_symlink_exists_real_filesystem(tmp_path, monkeypatch):
    """Real-world bug found via production verification on ir1: Path.resolve() is non-strict
    by default, so calling it on a /master path that doesn't exist at all does NOT raise --
    it just normalizes the (nonexistent) path, whose .name is literally the string 'master'.
    This made every non-member interface spuriously report master='master' (a truthy
    value). Exercised against a REAL filesystem (tmp_path), not a mock, to actually catch
    this class of bug."""
    iface_dir = tmp_path / "eno1"
    iface_dir.mkdir()
    # Deliberately create NO 'master' file/symlink at all -- the real eno1-4/ens1f0 case.
    monkeypatch.setattr(nt, "SYS_NET", tmp_path)
    assert nt.get_interface_master("eno1") is None


def test_get_interface_master_follows_real_symlink(tmp_path, monkeypatch):
    bridge_dir = tmp_path / "br0"
    bridge_dir.mkdir()
    member_dir = tmp_path / "eth0"
    member_dir.mkdir()
    (member_dir / "master").symlink_to(bridge_dir, target_is_directory=True)
    monkeypatch.setattr(nt, "SYS_NET", tmp_path)
    assert nt.get_interface_master("eth0") == "br0"
