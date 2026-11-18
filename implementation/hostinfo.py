"""Read-only host hardware/OS detection, stdlib only (no `psutil`, no third-party deps --
this package's installer never requires pip/internet access on the Virtualizor host itself).

Every metric is wrapped so a missing/unreadable source produces an explicit status instead of
a fabricated value. Three distinct outcomes are always distinguished, never collapsed into one
"unknown":
  - "unavailable"       -- the data source doesn't exist on this platform/kernel at all
  - "unsupported"       -- recognized but this platform/version doesn't expose it
  - "permission_denied" -- the source exists but this process couldn't read it

Nothing here touches Virtualizor, the renewal engine, or any VPS -- this is purely about the
host the software itself is running on.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from pathlib import Path

UNAVAILABLE = "unavailable"
UNSUPPORTED = "unsupported"
PERMISSION_DENIED = "permission_denied"


def _read_text(path: str):
    try:
        return Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return None
    except PermissionError:
        return PERMISSION_DENIED
    except OSError:
        return None


def get_os_info() -> dict:
    info = {"hostname": socket.gethostname(), "kernel": platform.release(),
            "architecture": platform.machine(), "distribution": None}
    text = _read_text("/etc/os-release")
    if text and text != PERMISSION_DENIED:
        fields = {}
        for line in text.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                fields[k] = v.strip('"')
        info["distribution"] = fields.get("PRETTY_NAME", UNAVAILABLE)
    else:
        info["distribution"] = UNAVAILABLE
    return info


def get_cpu_info() -> dict:
    logical = os.cpu_count() or None
    vendor = model = UNAVAILABLE
    physical_cores = None
    text = _read_text("/proc/cpuinfo")
    if text == PERMISSION_DENIED:
        return {"vendor": PERMISSION_DENIED, "model": PERMISSION_DENIED,
                "physical_cores": PERMISSION_DENIED, "logical_cpus": logical,
                "threads_per_core": PERMISSION_DENIED}
    if text is None:
        return {"vendor": UNAVAILABLE, "model": UNAVAILABLE, "physical_cores": UNAVAILABLE,
                "logical_cpus": logical, "threads_per_core": UNAVAILABLE}

    phys_ids = set()
    core_ids = set()
    for line in text.splitlines():
        if line.startswith("vendor_id") and vendor == UNAVAILABLE:
            vendor = line.split(":", 1)[1].strip()
        elif line.startswith("model name") and model == UNAVAILABLE:
            model = line.split(":", 1)[1].strip()
        elif line.startswith("physical id"):
            phys_ids.add(line.split(":", 1)[1].strip())
        elif line.startswith("core id"):
            core_ids.add(line.split(":", 1)[1].strip())

    physical_cores = len(core_ids) * max(len(phys_ids), 1) if core_ids else UNSUPPORTED
    threads_per_core = UNSUPPORTED
    if isinstance(physical_cores, int) and physical_cores > 0 and logical:
        threads_per_core = round(logical / physical_cores, 1)
    return {"vendor": vendor, "model": model, "physical_cores": physical_cores,
            "logical_cpus": logical or UNAVAILABLE, "threads_per_core": threads_per_core}


def get_memory_info() -> dict:
    text = _read_text("/proc/meminfo")
    if text == PERMISSION_DENIED:
        return {"total_mb": PERMISSION_DENIED, "available_mb": PERMISSION_DENIED,
                "swap_total_mb": PERMISSION_DENIED, "swap_free_mb": PERMISSION_DENIED}
    if text is None:
        return {"total_mb": UNAVAILABLE, "available_mb": UNAVAILABLE,
                "swap_total_mb": UNAVAILABLE, "swap_free_mb": UNAVAILABLE}
    values = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        val = parts[1].strip().split()[0]
        try:
            values[key] = int(val) // 1024  # kB -> MB
        except ValueError:
            continue
    return {
        "total_mb": values.get("MemTotal", UNAVAILABLE),
        "available_mb": values.get("MemAvailable", UNAVAILABLE),
        "swap_total_mb": values.get("SwapTotal", UNAVAILABLE),
        "swap_free_mb": values.get("SwapFree", UNAVAILABLE),
    }


def get_disk_info(paths: list[str] = None) -> list[dict]:
    """Reports usage for the given mount points (default: just `/`). Does not enumerate every
    mount in /proc/mounts automatically -- callers needing the full mount table should read
    /proc/mounts themselves; this keeps the default call cheap (a single statvfs)."""
    paths = paths or ["/"]
    results = []
    for p in paths:
        try:
            usage = shutil.disk_usage(p)
            results.append({
                "mount_point": p, "total_gb": round(usage.total / 1e9, 2),
                "used_gb": round(usage.used / 1e9, 2), "free_gb": round(usage.free / 1e9, 2),
                "percent_used": round(100 * usage.used / usage.total, 1) if usage.total else UNAVAILABLE,
            })
        except FileNotFoundError:
            results.append({"mount_point": p, "total_gb": UNAVAILABLE, "used_gb": UNAVAILABLE,
                             "free_gb": UNAVAILABLE, "percent_used": UNAVAILABLE})
        except PermissionError:
            results.append({"mount_point": p, "total_gb": PERMISSION_DENIED,
                             "used_gb": PERMISSION_DENIED, "free_gb": PERMISSION_DENIED,
                             "percent_used": PERMISSION_DENIED})
    return results


def get_uptime_and_load() -> dict:
    text = _read_text("/proc/uptime")
    uptime_seconds = UNAVAILABLE
    if text == PERMISSION_DENIED:
        uptime_seconds = PERMISSION_DENIED
    elif text:
        try:
            uptime_seconds = float(text.split()[0])
        except (ValueError, IndexError):
            uptime_seconds = UNAVAILABLE
    try:
        load1, load5, load15 = os.getloadavg()
        load = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except OSError:
        load = {"1m": UNSUPPORTED, "5m": UNSUPPORTED, "15m": UNSUPPORTED}
    return {"uptime_seconds": uptime_seconds, "load_average": load}


def get_cpu_memory_usage_snapshot() -> dict:
    """A cheap, instantaneous usage snapshot -- NOT a polling/sampling average (avoids the
    expensive-polling pitfall by design: one /proc read, no sleep, no background thread)."""
    mem = get_memory_info()
    cpu_percent = UNSUPPORTED  # a real instantaneous CPU% needs two /proc/stat samples apart
    # in time; deliberately not done here to avoid introducing a sleep into a status call.
    mem_percent = UNAVAILABLE
    if isinstance(mem["total_mb"], int) and isinstance(mem["available_mb"], int) and mem["total_mb"] > 0:
        used_mb = mem["total_mb"] - mem["available_mb"]
        mem_percent = round(100 * used_mb / mem["total_mb"], 1)
    return {"cpu_percent": cpu_percent, "memory_percent": mem_percent}


def get_temperature_info() -> dict:
    """Hardware temperature is notoriously unreliable in virtualized/cloud environments (most
    VPS/KVM guests expose no thermal zones at all) -- reported honestly as unavailable rather
    than guessed."""
    base = Path("/sys/class/thermal")
    if not base.is_dir():
        return {"available": False, "reason": UNAVAILABLE, "zones": []}
    zones = []
    try:
        for zone_dir in sorted(base.glob("thermal_zone*")):
            temp_path = zone_dir / "temp"
            type_path = zone_dir / "type"
            try:
                raw = temp_path.read_text().strip()
                celsius = int(raw) / 1000.0
                zone_type = type_path.read_text().strip() if type_path.exists() else "unknown"
                zones.append({"zone": zone_dir.name, "type": zone_type, "celsius": celsius})
            except (OSError, ValueError):
                continue
    except PermissionError:
        return {"available": False, "reason": PERMISSION_DENIED, "zones": []}
    if not zones:
        return {"available": False, "reason": UNSUPPORTED, "zones": []}
    return {"available": True, "reason": None, "zones": zones}


def _ipv4_for_interface(name: str):
    """IPv4 address for one interface via the SIOCGIFADDR ioctl (stdlib fcntl+socket, no
    subprocess). Returns None if the interface has no IPv4 address or the ioctl fails (e.g.
    the interface is down) -- never raises."""
    import fcntl
    import struct
    SIOCGIFADDR = 0x8915
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
            return socket.inet_ntoa(packed[20:24])
        finally:
            s.close()
    except OSError:
        return None


# Prefixes used by virtualization/container stacks for their auto-generated interfaces --
# NOT physical/management interfaces an operator cares about by default. On a Virtualizor
# KVM host with dozens of running VMs, every single guest gets its own per-VM device, which
# previously flooded the host-info screen with "dozens of virtual interfaces" (the exact
# complaint this classification fixes). This is a naming-convention heuristic, not a kernel-
# verified distinction -- an interface that doesn't match any of these prefixes is treated as
# physical/management by default, which errs toward SHOWING an unfamiliar interface rather
# than hiding something that might matter.
#
# v1.7.0 fix: `viifv` -- Virtualizor's OWN per-VM virtual interface naming convention,
# confirmed by direct operator observation on the real ir1 production host -- was missing
# from this list entirely, so every one of those interfaces was still shown on the "default"
# screen despite the feature claiming to hide virtual interfaces. This was the actual root
# cause of the reported bug, not a cosmetic rendering issue.
VIRTUAL_INTERFACE_PREFIXES = ("vnet", "tap", "virbr", "veth", "br-", "docker", "vif", "tun",
                              "viifv")


def is_virtual_interface(name: str) -> bool:
    return any(name.startswith(p) for p in VIRTUAL_INTERFACE_PREFIXES)


# Bridge-naming heuristic, same "err toward showing" philosophy as is_virtual_interface:
# Virtualizor's own host bridge is named `viifbr<N>` (e.g. `viifbr0`) -- distinct from the
# per-VM `viifv<N>` interfaces (already classified virtual above) despite sharing the
# `viif` prefix. A generic Linux bridge not managed by Virtualizor/Docker (already caught
# by the `br-`/`virbr` virtual prefixes) would still show up here as "bridge" via the
# `br` substring check -- intentional, since an operator-created bridge is exactly the
# kind of "genuinely important host interface" the default view should never hide.
def classify_interface_category(name: str) -> str:
    """Returns 'virtual', 'bridge', or 'physical' -- used by the advanced network-interface
    view's category filter. Never changes is_virtual_interface's own default-view decision;
    this is purely an additional display classification."""
    if is_virtual_interface(name):
        return "virtual"
    if "br" in name.lower():
        return "bridge"
    return "physical"


def filter_interfaces(interfaces: list[dict], category: str = None,
                       search: str = None) -> list[dict]:
    """Pure filtering helper for the advanced network-interface view: `category` narrows to
    exactly 'physical', 'bridge', or 'virtual' (None/'all' = no category filtering);
    `search` keeps only interfaces whose name contains the given substring
    (case-insensitive). Never touches actual interface data -- display filtering only."""
    result = interfaces
    if category and category != "all":
        result = [i for i in result if classify_interface_category(i["name"]) == category]
    if search:
        needle = search.lower()
        result = [i for i in result if needle in i["name"].lower()]
    return result


def get_interface_mac(name: str) -> str:
    """Reads /sys/class/net/<name>/address -- used as a strong (though not infallible)
    identity signal for detecting an interface RENAME (same physical device, new kernel
    name) versus a genuine replacement (different device). Returns UNAVAILABLE if the
    interface doesn't exist or has no MAC (e.g. some tunnel/virtual types)."""
    try:
        return (Path("/sys/class/net") / name / "address").read_text().strip()
    except OSError:
        return UNAVAILABLE


def _read_interface_counter(iface_dir: Path, stat_name: str):
    try:
        return int((iface_dir / "statistics" / stat_name).read_text().strip())
    except (OSError, ValueError):
        return UNAVAILABLE


def get_network_interfaces() -> list[dict]:
    """Enumerates interfaces via /sys/class/net (always present on Linux) and resolves each
    interface's IPv4 address via a local ioctl -- no subprocess, no `ip`/`ifconfig` dependency.
    IPv6 addresses are not resolved per-interface here (would need /proc/net/if_inet6 parsing);
    their absence is reported honestly as unsupported rather than silently omitted.

    Each entry is tagged `is_virtual` (see VIRTUAL_INTERFACE_PREFIXES) and `is_uninformative`
    (state/ipv4 both unavailable AND not virtual -- an interface the default view would
    otherwise show with nothing useful on it) -- callers (menu.py's host-info screen) use
    these to show only physical/management interfaces with real information by default, and
    put virtual/uninformative ones behind an explicit "show all" action."""
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        return []
    interfaces = []
    for iface_dir in sorted(net_dir.iterdir()):
        name = iface_dir.name
        if name == "lo":
            continue
        operstate_path = iface_dir / "operstate"
        state = UNAVAILABLE
        try:
            state = operstate_path.read_text().strip()
        except (OSError, PermissionError):
            pass
        ipv4 = _ipv4_for_interface(name)
        rx_bytes = _read_interface_counter(iface_dir, "rx_bytes")
        tx_bytes = _read_interface_counter(iface_dir, "tx_bytes")
        is_virtual = is_virtual_interface(name)
        is_uninformative = (not is_virtual and state == UNAVAILABLE and
                            (not ipv4) and rx_bytes == UNAVAILABLE)
        interfaces.append({"name": name, "state": state,
                            "ipv4": ipv4 if ipv4 else UNAVAILABLE, "ipv6": UNSUPPORTED,
                            "is_virtual": is_virtual, "is_uninformative": is_uninformative,
                            "rx_bytes": rx_bytes, "tx_bytes": tx_bytes})
    return interfaces


def get_host_timezone() -> str:
    """The host's SYSTEM timezone (distinct from this app's display-only timezone setting in
    menu.py, which never touches this value)."""
    try:
        link = os.readlink("/etc/localtime")
        return link.rsplit("zoneinfo/", 1)[-1]
    except (OSError, ValueError):
        return UNAVAILABLE


def collect_host_info(disk_paths: list[str] = None) -> dict:
    """Single entry point assembling everything above. Cheap: a handful of /proc and /sys
    reads, no subprocess calls, no polling loop -- safe to call on every dashboard refresh."""
    return {
        "os": get_os_info(),
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "disks": get_disk_info(disk_paths),
        "uptime": get_uptime_and_load(),
        "usage": get_cpu_memory_usage_snapshot(),
        "temperature": get_temperature_info(),
        "network_interfaces": get_network_interfaces(),
        "host_timezone": get_host_timezone(),
    }
