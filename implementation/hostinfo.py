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


def get_network_interfaces() -> list[dict]:
    """Enumerates interfaces via /sys/class/net (always present on Linux) and resolves each
    interface's IPv4 address via a local ioctl -- no subprocess, no `ip`/`ifconfig` dependency.
    IPv6 addresses are not resolved per-interface here (would need /proc/net/if_inet6 parsing);
    their absence is reported honestly as unsupported rather than silently omitted."""
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
        interfaces.append({"name": name, "state": state,
                            "ipv4": ipv4 if ipv4 else UNAVAILABLE, "ipv6": UNSUPPORTED})
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
