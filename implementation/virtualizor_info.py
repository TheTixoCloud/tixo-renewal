"""Virtualizor installation/version detection and Admin API connectivity probing, plus the
`act=vs` response-to-VPS-record parser used by inventory.py.

FIELD MAPPING PROVENANCE (v1.4.0): the field names used below were EMPIRICALLY CONFIRMED
against the real ir1 production installation via a strictly read-only, key-names-and-types-
only inspection (no customer values were ever read, logged, or printed) during v1.4.0
development. Confirmed real fields on that installation's `act=vs` response: a top-level `vs`
dict keyed by vpsid (string, numeric -- NOT by IP, correcting an earlier, wrong v1.3.0
assumption that was actually based on a DIFFERENT action, `act=listvs`, which turned out to
list physical servers/nodes, not VPSs, on this installation -- see writer.call_listvs_api's
docstring). Per-VPS fields confirmed present: vpsid, hostname, vps_name, ram, cores, space,
bandwidth, used_bandwidth, virt, suspended, machine_status, locked, serid, ips (a dict, not a
list), uuid, time.

This mapping is confirmed for ONE real installation/version only. Different Virtualizor
versions may differ (see detect_virtualizor_version()) -- this parser remains defensive: any
field it can't find is reported "unavailable", never defaulted to zero or fabricated, and a
record with no usable vpsid is excluded and the whole snapshot marked incomplete.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

UNAVAILABLE = "unavailable"

VIRTUALIZOR_VERSION_PATHS = (
    "/usr/local/virtualizor/version.php",
    "/usr/local/emps/etc/virtualizor/version.php",
)
UNIVERSAL_PHP_PATHS = (
    "/usr/local/virtualizor/universal.php",
    "/usr/local/emps/etc/virtualizor/universal.php",
)


def detect_virtualizor_installed() -> bool:
    return any(os.path.isfile(p) for p in UNIVERSAL_PHP_PATHS)


def detect_virtualizor_version() -> str:
    """Virtualizor's version.php typically defines a $version string. Parsed with a simple
    regex rather than executing PHP. Returns "unavailable" if the file is absent or the
    expected pattern isn't found -- never guesses a version number."""
    import re
    for path in VIRTUALIZOR_VERSION_PATHS:
        if not os.path.isfile(path):
            continue
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        m = re.search(r"""\$version\s*=\s*['"]([^'"]+)['"]""", text)
        if m:
            return m.group(1)
    return UNAVAILABLE


def detect_virtualization_technologies() -> list[str]:
    """Reports which hypervisor backends appear to be present on this host, WITHOUT assuming
    KVM is the only (or even present) one. Checked by looking for the corresponding
    Virtualizor-managed binary/directory conventions; any technology not detected is simply
    omitted from the list (never padded with a guessed default)."""
    checks = {
        "kvm": ["/usr/bin/qemu-system-x86_64", "/usr/libexec/qemu-kvm"],
        "openvz": ["/proc/vz"],
        "lxc": ["/usr/bin/lxc-start"],
        "proxmox": ["/etc/pve"],
    }
    found = []
    for tech, paths in checks.items():
        if any(os.path.exists(p) for p in paths):
            found.append(tech)
    return found


def probe_api_latency(creds, timeout: int = 10, call_listvs_api=None) -> dict:
    """Times a single read-only `act=listvs` call -- used purely as a lightweight
    connectivity/latency probe (NOT a VPS-listing call; see writer.call_listvs_api's
    docstring for why that's the right endpoint for this narrow purpose: it authenticates
    and returns quickly regardless of fleet size). Reuses writer.call_listvs_api (injectable
    for tests) -- never a second, parallel request path to the same credential-bearing
    endpoint."""
    if call_listvs_api is None:
        import writer as writer_mod
        call_listvs_api = writer_mod.call_listvs_api
    started = time.monotonic()
    try:
        result = call_listvs_api(creds, timeout=timeout)
    except Exception as e:  # noqa: BLE001 -- any failure here must still report latency/error, not raise
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {"ok": False, "latency_ms": elapsed_ms, "error": str(e)}
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if result.get("parsed") is None:
        return {"ok": False, "latency_ms": elapsed_ms, "error": "response was not valid JSON"}
    return {"ok": True, "latency_ms": elapsed_ms, "error": None}


def _to_decimal_or_none(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_listvs_response(parsed: dict) -> dict:
    """Converts a parsed `act=listvs` JSON body into a list of per-VPS dicts plus a
    completeness verdict. Never raises on malformed input -- always returns a result dict.

    Returns:
      {"complete": bool, "records": [...], "skipped_count": int, "error": str|None}

    A record is only included if it carries a parseable `vpsid` -- vpsid is the sole reliable
    cross-reference key into this project's renewal ledger (shadow_monitor.py's `identities`
    table), so a VPS record Virtualizor returned without one is NOT silently included with a
    guessed id; it's counted in `skipped_count` and `complete` is set False, since an inventory
    sync missing even one identity must never be mistaken for "this VPS was deleted" by the
    change-tracking logic in inventory.py.
    """
    if not isinstance(parsed, dict):
        return {"complete": False, "records": [], "skipped_count": 0,
                "error": "top-level response was not a JSON object"}
    vs = parsed.get("vs")
    if vs is None:
        return {"complete": False, "records": [], "skipped_count": 0,
                "error": "response had no 'vs' key"}
    if not isinstance(vs, dict):
        return {"complete": False, "records": [], "skipped_count": 0,
                "error": f"'vs' was {type(vs).__name__}, expected an object keyed by vpsid"}

    records = []
    skipped = 0
    for _key, raw in vs.items():
        if not isinstance(raw, dict):
            skipped += 1
            continue
        vpsid_raw = raw.get("vpsid")
        try:
            vpsid = int(vpsid_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue

        quota = _to_decimal_or_none(raw.get("bandwidth"))
        used = _to_decimal_or_none(raw.get("used_bandwidth"))

        locked_raw = raw.get("locked")
        locked = None if locked_raw is None else str(locked_raw) not in ("", "0", "none")

        if str(raw.get("suspended", "0")) in ("1", "true", "True"):
            status = "suspended"
        elif "machine_status" in raw:
            status = raw.get("machine_status") or UNAVAILABLE
        else:
            status = UNAVAILABLE

        ips_raw = raw.get("ips")
        if isinstance(ips_raw, dict):
            ip_count = len(ips_raw)
        elif isinstance(ips_raw, list):
            ip_count = len(ips_raw)
        else:
            ip_count = UNAVAILABLE

        records.append({
            "vpsid": vpsid,
            "hostname": raw.get("hostname") or raw.get("vps_name", UNAVAILABLE),
            "status": status,
            "locked": locked if locked is not None else UNAVAILABLE,
            "ip_count": ip_count,
            "ram_mb": raw.get("ram", UNAVAILABLE),
            "cores": raw.get("cores", UNAVAILABLE),
            "disk_gb": raw.get("space", UNAVAILABLE),
            "quota_gb": quota if quota is not None else UNAVAILABLE,
            "used_gb": used if used is not None else UNAVAILABLE,
            "virtualization": raw.get("virt", UNAVAILABLE),
            "node_serid": raw.get("serid", UNAVAILABLE),
        })

    complete = skipped == 0 and len(records) > 0
    error = None if complete else (
        f"{skipped} record(s) had no usable vpsid" if skipped else "no usable records found"
    )
    return {"complete": complete, "records": records, "skipped_count": skipped, "error": error}
