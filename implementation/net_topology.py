"""Portable Linux network interface topology discovery, for choosing a safe, non-overlapping
host-traffic-accounting interface WITHOUT hardcoding any interface name (e.g. the real
ir1 production interface `ens1f1` must never be assumed to exist elsewhere) and WITHOUT
relying on naming-prefix heuristics alone (`eno*`/`ens*`/`enp*`/`bond*` conventions vary by
distro/hardware and are used only as a weak hint, never as proof).

Every function here is STRICTLY READ-ONLY: it reads /sys/class/net, /proc/net/route,
/proc/net/ipv6_route, and /proc/net/vlan/config -- never writes, never runs `ip`/`brctl`/
any subprocess, never touches routing or interface configuration. No packet capture, no
high-frequency polling (this is called on-demand by the menu/CLI and periodically by
hostnet_sync.py's own bounded, low-frequency schedule, never in a tight loop).
"""
from __future__ import annotations

from pathlib import Path

SYS_NET = Path("/sys/class/net")

ROLE_PHYSICAL = "physical"
ROLE_BRIDGE = "bridge"
ROLE_BOND_MASTER = "bond_master"
ROLE_BOND_MEMBER = "bond_member"
ROLE_BRIDGE_MEMBER = "bridge_member"
ROLE_VLAN = "vlan"
ROLE_UNKNOWN = "unknown"


def get_interface_master(name: str) -> str:
    """The bond/bridge this interface is a MEMBER of, via the kernel-maintained
    /sys/class/net/<name>/master symlink -- the same mechanism for both bond and bridge
    membership. Returns None if the interface has no master (not a member of anything)."""
    link = SYS_NET / name / "master"
    try:
        target = link.resolve()
        return target.name
    except (OSError, RuntimeError):
        return None


def is_bridge(name: str) -> bool:
    return (SYS_NET / name / "bridge").is_dir()


def is_bond_master(name: str) -> bool:
    return (SYS_NET / name / "bonding").is_dir()


def read_vlan_parents() -> dict:
    """Parses /proc/net/vlan/config (format: 'ifname | VID | parent-device', e.g.
    'eth0.100      | 100  | eth0') -- returns {vlan_name: parent_name}. Returns {} if the
    8021q module isn't loaded or the file doesn't exist (no VLANs configured), never raises."""
    path = Path("/proc/net/vlan/config")
    if not path.is_file():
        return {}
    parents = {}
    try:
        for line in path.read_text().splitlines():
            if "|" not in line or "VLAN Dev name" in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3 and parts[0]:
                parents[parts[0]] = parts[2]
    except OSError:
        return {}
    return parents


def _ipv4_default_route_interfaces() -> set:
    """Parses /proc/net/route: destination field '00000000' (hex, any byte order -- a
    default route's destination is always all-zero regardless of endianness) means a
    default route. Returns the set of interface names carrying at least one IPv4 default
    route (there can legitimately be more than one, e.g. ECMP or multiple default gateways
    with different metrics -- never assumed to be exactly one)."""
    path = Path("/proc/net/route")
    if not path.is_file():
        return set()
    ifaces = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    if len(lines) < 2:
        return set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        iface, dest = fields[0], fields[1]
        if dest == "00000000":
            ifaces.add(iface)
    return ifaces


def _ipv6_default_route_interfaces() -> set:
    """Parses /proc/net/ipv6_route: a default route has destination
    '00000000000000000000000000000000' with prefix length '00'. Field layout (whitespace-
    separated): dest dest_plen src src_plen next_hop metric refcnt use flags ifname."""
    path = Path("/proc/net/ipv6_route")
    if not path.is_file():
        return set()
    ifaces = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    for line in lines:
        fields = line.split()
        if len(fields) < 10:
            continue
        dest, dest_plen = fields[0], fields[1]
        ifname = fields[9]
        if dest == "0" * 32 and dest_plen == "00":
            ifaces.add(ifname)
    return ifaces


def get_default_route_interfaces() -> dict:
    return {"ipv4": _ipv4_default_route_interfaces(), "ipv6": _ipv6_default_route_interfaces()}


def classify_interface(name: str, vlan_parents: dict = None) -> dict:
    """Returns the topology role and relationships for one interface -- role is informative
    only; it is NEVER used alone to prove an interface carries VPS traffic (see
    select_accounting_candidates, which uses this plus routing/address evidence)."""
    vlan_parents = vlan_parents if vlan_parents is not None else read_vlan_parents()
    master = get_interface_master(name)

    if name in vlan_parents:
        return {"name": name, "role": ROLE_VLAN, "master": master, "parent": vlan_parents[name]}
    if is_bond_master(name):
        return {"name": name, "role": ROLE_BOND_MASTER, "master": None, "parent": None}
    if is_bridge(name):
        return {"name": name, "role": ROLE_BRIDGE, "master": None, "parent": None}
    if master:
        # A member interface's role depends on what kind of master it has.
        if is_bond_master(master):
            return {"name": name, "role": ROLE_BOND_MEMBER, "master": master, "parent": None}
        if is_bridge(master):
            return {"name": name, "role": ROLE_BRIDGE_MEMBER, "master": master, "parent": None}
        return {"name": name, "role": ROLE_UNKNOWN, "master": master, "parent": None}
    # Has its own hardware device backing (a real NIC) -- the best positive signal of
    # "physical", though still just a heuristic (some virtual drivers also expose /device).
    has_device = (SYS_NET / name / "device").exists()
    return {"name": name, "role": ROLE_PHYSICAL if has_device else ROLE_UNKNOWN,
            "master": None, "parent": None}


def build_topology(interface_names: list[str]) -> dict:
    vlan_parents = read_vlan_parents()
    return {name: classify_interface(name, vlan_parents) for name in interface_names}


def select_accounting_candidates(interfaces: list[dict]) -> dict:
    """The core topology-aware selection. `interfaces` is hostinfo.get_network_interfaces()'s
    output (already carries is_virtual/is_uninformative/ipv4/rx_bytes/tx_bytes per entry).

    Returns {"auto_selected": str|None, "candidates": [...], "excluded": [...]}.

    Exclusion rule (unambiguous, always applied): a bond or bridge MEMBER interface is
    excluded entirely -- its master/aggregate already represents its traffic, so including
    both would double-count. This is the one topology relationship resolved automatically,
    because for a member/master pair there is no genuine deployment-dependent ambiguity:
    the aggregate is always the correct superset.

    VLAN-vs-parent is a GENUINE ambiguity (which one represents "VPS traffic" depends on
    the deployment -- a VLAN might carry only VPS traffic while the physical parent also
    carries management traffic, or vice versa) and is therefore never auto-collapsed: both
    remain candidates, with the relationship surfaced for the operator to decide.

    Auto-selection (`auto_selected` non-None) only happens when exactly one non-virtual,
    non-uninformative, non-excluded candidate remains. Default-route membership is attached
    as evidence/reasoning on each candidate, never used by itself to break a tie between
    multiple remaining candidates."""
    names = [i["name"] for i in interfaces]
    topology = build_topology(names)
    routes = get_default_route_interfaces()

    candidates = []
    excluded = []
    for iface in interfaces:
        name = iface["name"]
        topo = topology[name]
        if iface.get("is_virtual"):
            excluded.append({**iface, **topo, "exclusion_reason": "virtual (per-VM) interface"})
            continue
        if iface.get("is_uninformative"):
            excluded.append({**iface, **topo, "exclusion_reason": "no usable state/IP/counter data"})
            continue
        if topo["role"] in (ROLE_BOND_MEMBER, ROLE_BRIDGE_MEMBER):
            excluded.append({**iface, **topo,
                             "exclusion_reason": f"member of {topo['master']} -- would double-count "
                                                  f"against the aggregate"})
            continue
        is_default_v4 = name in routes["ipv4"]
        is_default_v6 = name in routes["ipv6"]
        candidates.append({**iface, **topo, "is_default_route_v4": is_default_v4,
                           "is_default_route_v6": is_default_v6})

    # Sort by observed traffic (most active first) purely for display order -- never the
    # basis for automatic selection by itself.
    candidates.sort(key=lambda c: (c.get("rx_bytes") or 0) + (c.get("tx_bytes") or 0), reverse=True)

    auto_selected = candidates[0]["name"] if len(candidates) == 1 else None
    return {"auto_selected": auto_selected, "candidates": candidates, "excluded": excluded}


def recommendation_reason(candidate: dict) -> str:
    parts = [f"role={candidate['role']}"]
    if candidate.get("is_default_route_v4"):
        parts.append("carries the IPv4 default route")
    if candidate.get("is_default_route_v6"):
        parts.append("carries the IPv6 default route")
    if candidate.get("parent"):
        parts.append(f"VLAN on top of {candidate['parent']}")
    if candidate.get("ipv4") and candidate["ipv4"] != "unavailable":
        parts.append(f"has address {candidate['ipv4']}")
    return ", ".join(parts)
