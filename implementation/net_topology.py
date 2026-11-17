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
    membership. Returns None if the interface has no master (not a member of anything).

    BUG FIX: Path.resolve() is non-strict by default -- on a symlink path that doesn't
    exist at all, it does NOT raise; it just normalizes the (nonexistent) path and returns
    it, whose .name is literally the string "master" (the path component itself). This
    previously caused every non-member interface to be misreported as master="master"
    (a truthy value), found via real-world verification against ir1's actual interfaces
    (eno1-4/ens1f0, which have no master symlink at all). Checking is_symlink() first
    avoids this entirely."""
    link = SYS_NET / name / "master"
    if not link.is_symlink():
        return None
    try:
        return link.resolve().name
    except (OSError, RuntimeError):
        return None


def is_bridge(name: str) -> bool:
    return (SYS_NET / name / "bridge").is_dir()


def is_bond_master(name: str) -> bool:
    return (SYS_NET / name / "bonding").is_dir()


def has_hardware_device(name: str) -> bool:
    """A real hardware NIC backing this interface (vs a purely software construct like a
    bridge, bond, or VLAN sub-interface) -- the same positive physical-ness signal
    classify_interface()'s fallback branch already uses, exposed here so bridge-membership
    evaluation can apply it to MEMBER interfaces too (classify_interface's own fallback is
    only reached for interfaces with no master at all)."""
    return (SYS_NET / name / "device").exists()


def read_bridge_members(bridge_name: str) -> list[str]:
    """Lists a bridge's member interfaces via /sys/class/net/<bridge>/brif/ -- the standard,
    authoritative kernel-maintained bridge membership directory (one symlink per member).
    Returns [] if `bridge_name` isn't a bridge or has no members."""
    brif_dir = SYS_NET / bridge_name / "brif"
    if not brif_dir.is_dir():
        return []
    try:
        return sorted(p.name for p in brif_dir.iterdir())
    except OSError:
        return []


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

    BOND members are excluded unconditionally: the Linux bonding driver maintains the
    master's own counters as the true sum of all slave traffic, so a bond master is always
    the correct, non-overlapping aggregate -- there is no genuine ambiguity here.

    BRIDGE members are handled differently, based on a real-world finding (verified against
    ir1's actual topology, read-only, by comparing counter values): a Linux bridge DEVICE's
    own rx/tx counters only count frames locally delivered to the bridge's own address --
    NOT the full set of frames switched between its ports. A bridge's physical (hardware-
    backed, non-virtual) member interface, by contrast, sees every frame on the physical
    wire, which IS the correct "total external traffic" figure. So: when a bridge has
    EXACTLY ONE physical, non-virtual member, that member is kept as a candidate and the
    BRIDGE DEVICE ITSELF is excluded (the reverse of the bond case) -- selecting the bridge
    device there would silently and drastically UNDERSTATE real traffic, not merely
    duplicate it. When a bridge has zero or multiple physical members, this resolution does
    not apply and both the bridge and its physical member(s) remain candidates, correctly
    surfaced as ambiguous.

    A DOWN interface (administratively or physically down -- carries no live traffic by
    definition) is always excluded from candidacy, regardless of name. This is a portable,
    name-agnostic signal that lets automatic selection resolve correctly even on a host
    with several unused/disabled physical NICs alongside the one real uplink.

    VLAN-vs-parent is a GENUINE ambiguity (which one represents "VPS traffic" depends on
    the deployment) and is therefore never auto-collapsed: both remain candidates, with the
    relationship surfaced for the operator to decide. The same applies to multiple
    candidates with independent default routes, or multiple NICs with no default route at
    all (e.g. separate management/VPS networks).

    Auto-selection (`auto_selected` non-None) only happens when exactly one real candidate
    remains after all of the above. Default-route membership is attached as evidence/
    reasoning on each candidate, never used by itself to break a tie between multiple real
    candidates, and never fabricated as "confidence" when the evidence doesn't support it."""
    names = [i["name"] for i in interfaces]
    topology = build_topology(names)
    routes = get_default_route_interfaces()
    iface_by_name = {i["name"]: i for i in interfaces}

    # Pre-resolve, for every bridge present, how many of its members are "physical" (hardware-
    # backed AND not matching the virtual-interface naming heuristic) -- this is what decides
    # whether the bridge device itself should be excluded in favor of that one member.
    bridge_physical_members = {}
    for name, topo in topology.items():
        if topo["role"] == ROLE_BRIDGE:
            members = read_bridge_members(name)
            physical = [m for m in members if has_hardware_device(m)
                        and not iface_by_name.get(m, {}).get("is_virtual", False)]
            bridge_physical_members[name] = physical

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
        if iface.get("state") == "down":
            excluded.append({**iface, **topo, "exclusion_reason": "interface is down -- cannot "
                                                                    "be the active accounting source"})
            continue
        if topo["role"] == ROLE_BOND_MEMBER:
            excluded.append({**iface, **topo,
                             "exclusion_reason": f"member of bond {topo['master']} -- the bond "
                                                  f"master's own counters already reflect the true "
                                                  f"sum of all slave traffic"})
            continue
        if topo["role"] == ROLE_BRIDGE:
            physical_members = bridge_physical_members.get(name, [])
            if len(physical_members) == 1:
                excluded.append({**iface, **topo,
                                 "exclusion_reason": f"bridge device -- only counts traffic locally "
                                                      f"delivered to this bridge's own address, NOT "
                                                      f"the full wire traffic; its one physical member "
                                                      f"({physical_members[0]}) represents the correct "
                                                      f"total (verified empirically: {name}'s own "
                                                      f"counters are orders of magnitude smaller)"})
                continue
            # Zero or multiple physical members: this resolution doesn't apply -- fall through
            # and let the bridge remain a candidate alongside its member(s), genuinely ambiguous.
        # ROLE_BRIDGE_MEMBER interfaces are deliberately NOT excluded here -- see the bridge
        # branch above, which is where that relationship is actually resolved (from the
        # bridge's side, since it needs to count ALL of the bridge's members to decide).
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
