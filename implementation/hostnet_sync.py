"""Orchestrates host network-traffic sampling: interface selection/confirmation, a single
non-overlapping local-only sample, and persistence via hostnet.py. Entirely independent of
inventory_sync.py and the renewal engine -- its own lock file, no API calls of any kind
(reads only local /sys/class/net counters), bounded and fast by construction (a handful of
file reads, no network, no sleep).
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path

DEFAULT_SAMPLE_INTERVAL_MIN = 5


class HostnetLockHeld(Exception):
    pass


def _lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, "hostnet-sync.lock")


def acquire_lock(state_dir: str):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fh = open(_lock_path(state_dir), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EACCES, errno.EAGAIN):
            raise HostnetLockHeld("another host-network sample is already running")
        raise
    return fh


def release_lock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "hostnet-settings.json")


def load_settings(config_dir: str) -> dict:
    path = _settings_path(config_dir)
    if not os.path.exists(path):
        return {"interface": None, "sample_interval_minutes": DEFAULT_SAMPLE_INTERVAL_MIN}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"interface": None, "sample_interval_minutes": DEFAULT_SAMPLE_INTERVAL_MIN}
    data.setdefault("interface", None)
    data.setdefault("sample_interval_minutes", DEFAULT_SAMPLE_INTERVAL_MIN)
    return data


def suggest_interfaces(get_network_interfaces=None) -> dict:
    """Topology-aware candidate selection (see net_topology.py) -- never hardcodes any
    interface name, never relies on naming prefixes alone, and automatically excludes bond/
    bridge MEMBER interfaces (the aggregate already represents them) while surfacing genuine
    ambiguities (VLAN-vs-parent, multiple default routes, separate management/VPS networks)
    for explicit operator choice rather than guessing. Returns
    {"auto_selected": str|None, "candidates": [...], "excluded": [...]}."""
    if get_network_interfaces is None:
        import hostinfo
        get_network_interfaces = hostinfo.get_network_interfaces
    import net_topology
    return net_topology.select_accounting_candidates(get_network_interfaces())


def save_monitored_interface(config_dir: str, interface_name: str, confirmed: bool,
                              get_network_interfaces=None, state_dir: str = None) -> None:
    """Requires `confirmed=True` -- this is the "clear operator confirmation" gate; never
    called automatically by any sampling/scheduling code path. Refuses to save a known-
    virtual interface (a per-VM device would never be a meaningful host-traffic source).

    Persists the interface's current MAC address alongside its name -- the identity signal
    used later by revalidate_interface() to distinguish a RENAME (same device, new kernel
    name) from a genuine REPLACEMENT (different device), so history is never silently
    merged across two different physical devices that happened to share a name."""
    if not confirmed:
        raise ValueError("save_monitored_interface requires explicit operator confirmation")
    import hostinfo as hostinfo_mod
    if hostinfo_mod.is_virtual_interface(interface_name):
        raise ValueError(f"{interface_name!r} looks like a virtual per-VM interface, "
                          f"not a physical/management one -- refusing to monitor it")
    settings = load_settings(config_dir)
    previous_interface = settings.get("interface")
    settings["interface"] = interface_name
    settings["mac_address"] = hostinfo_mod.get_interface_mac(interface_name)
    path = _settings_path(config_dir)
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)

    if state_dir is not None:
        import hostnet
        conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
        event_type = (hostnet.SOURCE_EVENT_CONFIGURED if previous_interface is None
                      else hostnet.SOURCE_EVENT_REPLACED)
        hostnet.record_source_event(conn, event_type, old_interface=previous_interface,
                                     new_interface=interface_name,
                                     note="operator-confirmed selection")


def revalidate_interface(config_dir: str, state_dir: str, list_interface_names=None,
                          get_mac=None) -> dict:
    """Checks whether the configured interface still exists. If it has disappeared, looks
    for a RENAME by matching the persisted MAC address against every currently-existing
    interface -- an exact MAC match is strong, reliable rename evidence (same physical
    device, new kernel-assigned name), auto-updates the active configuration to the new
    name, and records a RENAME_DETECTED event. If no MAC match is found, the device is
    treated as genuinely gone (REPLACED/missing) -- configuration is cleared back to
    "not configured" and an INTERFACE_MISSING event is recorded; the operator must
    explicitly re-confirm a new interface (never auto-guessed). History is NEVER merged
    across different names without this explicit, evidenced rename path -- a replacement
    always starts a fresh, separate accounting segment (hostnet.py's cumulative table is
    keyed by exact interface name, so a new name is structurally a new segment)."""
    if list_interface_names is None:
        import hostinfo
        list_interface_names = lambda: [i["name"] for i in hostinfo.get_network_interfaces()]
    if get_mac is None:
        import hostinfo
        get_mac = hostinfo.get_interface_mac

    settings = load_settings(config_dir)
    configured = settings.get("interface")
    if not configured:
        return {"status": "NOT_CONFIGURED"}

    current_names = list_interface_names()
    if configured in current_names:
        return {"status": "OK", "interface": configured}

    import hostnet
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    stored_mac = settings.get("mac_address")
    if stored_mac and stored_mac != "unavailable":
        for candidate in current_names:
            if get_mac(candidate) == stored_mac:
                hostnet.record_source_event(
                    conn, hostnet.SOURCE_EVENT_RENAME_DETECTED,
                    old_interface=configured, new_interface=candidate,
                    note=f"MAC address match ({stored_mac})")
                settings["interface"] = candidate
                path = _settings_path(config_dir)
                Path(path).write_text(json.dumps(settings, indent=2) + "\n")
                return {"status": "RENAMED", "old_interface": configured, "new_interface": candidate}

    hostnet.record_source_event(conn, hostnet.SOURCE_EVENT_INTERFACE_MISSING,
                                 old_interface=configured, new_interface=None,
                                 note="interface no longer present and no MAC match found")
    settings["interface"] = None
    path = _settings_path(config_dir)
    Path(path).write_text(json.dumps(settings, indent=2) + "\n")
    return {"status": "MISSING", "old_interface": configured}


def run_hostnet_sample(state_dir: str, config_dir: str, read_counters_fn=None,
                        list_interface_names=None, get_mac=None) -> dict:
    """One bounded, local-only sample. Returns {"status": ...}. Never calls the Virtualizor
    API, never touches the renewal ledger or inventory database, never requires
    WRITE_APPROVED. Revalidates the configured interface first (rename detection/missing-
    interface handling -- see revalidate_interface()) before sampling. `list_interface_names`/
    `get_mac` are injectable for tests, matching revalidate_interface()'s own signature."""
    revalidation = revalidate_interface(config_dir, state_dir, list_interface_names=list_interface_names,
                                         get_mac=get_mac)
    if revalidation["status"] == "NOT_CONFIGURED":
        return {"status": "NOT_CONFIGURED", "error": "no interface configured yet"}
    if revalidation["status"] == "MISSING":
        return {"status": "NOT_CONFIGURED",
                "error": f"interface {revalidation['old_interface']!r} no longer exists and "
                         f"no rename match was found -- re-configure a new interface"}

    settings = load_settings(config_dir)
    interface = settings.get("interface")
    if not interface:
        return {"status": "NOT_CONFIGURED", "error": "no interface configured yet"}

    try:
        lock = acquire_lock(state_dir)
    except HostnetLockHeld as e:
        return {"status": "ALREADY_RUNNING", "error": str(e)}

    try:
        if read_counters_fn is None:
            import hostinfo
            from pathlib import Path as _P
            iface_dir = _P("/sys/class/net") / interface
            def read_counters_fn():
                rx = hostinfo._read_interface_counter(iface_dir, "rx_bytes")
                tx = hostinfo._read_interface_counter(iface_dir, "tx_bytes")
                return rx, tx
        try:
            rx_bytes, tx_bytes = read_counters_fn()
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}
        if not isinstance(rx_bytes, int) or not isinstance(tx_bytes, int):
            return {"status": "ERROR", "error": f"interface {interface!r} counters unavailable"}

        import hostnet
        conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
        result = hostnet.record_sample(conn, interface, rx_bytes, tx_bytes)
        return {"status": "OK", "error": None, "sample": result}
    finally:
        release_lock(lock)
