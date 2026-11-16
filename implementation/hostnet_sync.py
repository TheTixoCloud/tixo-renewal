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


def suggest_interfaces(get_network_interfaces=None) -> list[dict]:
    """Candidate physical/management interfaces for the operator to choose from, sorted by
    total observed traffic (most active first -- a reasonable, non-authoritative hint, never
    an automatic silent choice). Virtual (per-VM) interfaces are excluded entirely; this
    function does NOT attempt to detect a bridge-vs-member double-counting relationship --
    that judgment call is left to the operator, who sees all candidates and picks exactly
    one, never both a bridge and its physical member automatically."""
    if get_network_interfaces is None:
        import hostinfo
        get_network_interfaces = hostinfo.get_network_interfaces
    interfaces = get_network_interfaces()
    candidates = [i for i in interfaces if not i.get("is_virtual") and not i.get("is_uninformative")]
    candidates.sort(key=lambda i: (i.get("rx_bytes") or 0) + (i.get("tx_bytes") or 0), reverse=True)
    return candidates


def save_monitored_interface(config_dir: str, interface_name: str, confirmed: bool,
                              get_network_interfaces=None) -> None:
    """Requires `confirmed=True` -- this is the "clear operator confirmation" gate; never
    called automatically by any sampling/scheduling code path. Refuses to save a known-
    virtual interface (a per-VM device would never be a meaningful host-traffic source)."""
    if not confirmed:
        raise ValueError("save_monitored_interface requires explicit operator confirmation")
    if get_network_interfaces is None:
        import hostinfo
        get_network_interfaces = hostinfo.get_network_interfaces
    import hostinfo as hostinfo_mod
    if hostinfo_mod.is_virtual_interface(interface_name):
        raise ValueError(f"{interface_name!r} looks like a virtual per-VM interface, "
                          f"not a physical/management one -- refusing to monitor it")
    settings = load_settings(config_dir)
    settings["interface"] = interface_name
    path = _settings_path(config_dir)
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)


def run_hostnet_sample(state_dir: str, config_dir: str, read_counters_fn=None) -> dict:
    """One bounded, local-only sample. Returns {"status": ...}. Never calls the Virtualizor
    API, never touches the renewal ledger or inventory database, never requires
    WRITE_APPROVED."""
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
