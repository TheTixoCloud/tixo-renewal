"""Virtualizor installation/version detection and Admin API connectivity probing, plus the
listvs-response-to-VPS-record parser used by inventory.py.

HONESTY NOTE on the listvs response shape: the only empirically confirmed fact about a real
Virtualizor Admin API `act=listvs` response (see implementation/writer.py's
sanitize_json_structure history and tests/test_writer.py) is that the top-level `vs` key is a
dict KEYED BY IP ADDRESS, not by vpsid, and that a `bandwidth` field exists inside each VPS's
record. Every other per-VPS field name used below (hostname, status, ips, ram, cores, space,
virt, used_bandwidth) comes from Virtualizor's publicly documented Admin API field names, not
from a live-verified response on this project's own production host. This parser is therefore
written defensively: any field it can't find is reported as "unavailable", never defaulted to
zero or fabricated. If your installation's real response differs, `diagnose-api` (writer.py)
remains the sanctioned way to inspect the actual (sanitized) shape before trusting this parser.
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
    """Times a single read-only `act=listvs` call. Reuses writer.call_listvs_api (injectable
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
                "error": f"'vs' was {type(vs).__name__}, expected an object keyed by IP"}

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
        records.append({
            "vpsid": vpsid,
            "hostname": raw.get("hostname", UNAVAILABLE),
            "status": "suspended" if str(raw.get("suspended", "0")) in ("1", "true", "True")
                       else raw.get("status", UNAVAILABLE),
            "ips": raw.get("ips") if isinstance(raw.get("ips"), list) else UNAVAILABLE,
            "ram_mb": raw.get("ram", UNAVAILABLE),
            "cores": raw.get("cores", UNAVAILABLE),
            "disk_gb": raw.get("space", UNAVAILABLE),
            "quota_gb": quota if quota is not None else UNAVAILABLE,
            "used_gb": used if used is not None else UNAVAILABLE,
            "virtualization": raw.get("virt", UNAVAILABLE),
        })

    complete = skipped == 0 and len(records) > 0
    error = None if complete else (
        f"{skipped} record(s) had no usable vpsid" if skipped else "no usable records found"
    )
    return {"complete": complete, "records": records, "skipped_count": skipped, "error": error}
