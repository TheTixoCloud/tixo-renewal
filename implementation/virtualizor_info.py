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

        # STATUS MAPPING (v1.5.0 correction): `machine_status` was found, by reading
        # Virtualizor's own virtualizor.sql schema and cross-referencing the files that
        # reference it (add_baremetal_theme.php, list_baremetals_theme.php), to be a
        # BAREMETAL-specific field -- unrelated to VPS running/stopped state, which explains
        # why it was always "0" for every VPS in v1.4.0. The `vps` table itself has NO
        # running/stopped column at all (confirmed via the schema); Virtualizor determines
        # live VM state dynamically from the hypervisor, not from this bulk listing endpoint.
        # `suspended` IS a real, confirmed schema column (billing-level admin suspension,
        # distinct from "is the VM process currently running"). Per the explicit instruction
        # to never guess a numeric status mapping: only SUSPENDED is ever reported with
        # confidence here; everything else is UNKNOWN, not RUNNING, until a reliable
        # live-state source is confirmed (see docs on this limitation).
        if str(raw.get("suspended", "0")) in ("1", "true", "True"):
            status = "suspended"
        else:
            status = "unknown"

        # IP FIELD MAPPING (v1.6.0, confirmed empirically against ir1, read-only, shape-only
        # inspection -- never bulk-logged): `ips` is a dict keyed by an internal numeric ID
        # (Virtualizor's own ip-record id, NOT the IP itself), with each VALUE being the
        # actual IPv4 address string. There is no separate API-confirmed "primary IP" flag --
        # the first entry (dict insertion order, as JSON preserves it) is used as a best-
        # effort "primary" convention, documented as such, never asserted as authoritative.
        # IPs are never inferred from the vpsid or any host interface name.
        ips_raw = raw.get("ips")
        ip_list = UNAVAILABLE
        ip_primary = UNAVAILABLE
        ip_count = UNAVAILABLE
        if isinstance(ips_raw, dict) and ips_raw:
            ip_list = list(ips_raw.values())
            ip_primary = ip_list[0]
            ip_count = len(ip_list)
        elif isinstance(ips_raw, dict):
            ip_list, ip_count = [], 0
        elif isinstance(ips_raw, list):
            ip_list = ips_raw
            ip_primary = ip_list[0] if ip_list else UNAVAILABLE
            ip_count = len(ip_list)

        records.append({
            "vpsid": vpsid,
            "hostname": raw.get("hostname") or raw.get("vps_name", UNAVAILABLE),
            "status": status,
            # RUNTIME STATUS (v1.7.2): distinct from `status` above, which only ever reports
            # the ADMINISTRATIVE billing-suspension flag (see the comment above) and never a
            # real power state. `runtime_status` defaults to "unavailable" here -- it is only
            # ever populated by merging in fetch_vs_status_bulk()'s result (see
            # fetch_vs_inventory below), which is the one confirmed authoritative source for
            # VM running/stopped/suspended state. A record that never gets enriched (e.g. the
            # vs_status call failed, or this parse path is used standalone/in a test without
            # enrichment) must keep reporting "unavailable", never silently "running".
            "runtime_status": UNAVAILABLE,
            "network_suspended_reason": None,
            "locked": locked if locked is not None else UNAVAILABLE,
            "ip_count": ip_count,
            "ip_primary": ip_primary,
            "ip_list": ip_list,
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


# RUNTIME STATUS MAPPING (v1.7.2, confirmed empirically against ir1 AND against Virtualizor's
# own published admin-API documentation for the dedicated `act=vs&vs_status=<ids>` action --
# a SEPARATE, documented bulk endpoint from the plain `act=vs` listing, read-only, GET-only, no
# vpsid-specific mutation parameter). Confirmed real values seen on ir1's live fleet of 60 VPS:
# 59 VPS reported status=1 (documented "VPS is on"); one VPS (observed used_cpu=0.00, net_in=0)
# reported status=1 too at the VM-power level but carried a non-false `network_status` entry
# (`{"id": "2", "reason": "bw", "date": ...}`) -- i.e. the VM process was on, but Virtualizor
# had network-suspended it for a bandwidth reason. This is a DIFFERENT concept from VM power
# state (see `network_suspended_reason` below) and must never be conflated with it. Only the
# three documented codes are ever trusted; any other integer is reported "unavailable" rather
# than guessed, per the explicit instruction to never fabricate a runtime state.
RUNTIME_STATUS_CODES = {0: "stopped", 1: "running", 2: "suspended"}


def map_runtime_status_code(code) -> str:
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return UNAVAILABLE
    return RUNTIME_STATUS_CODES.get(code_int, UNAVAILABLE)


def parse_vs_status_response(parsed: dict) -> dict:
    """Parses the dedicated, documented `act=vs&vs_status=<comma-separated vpsids>` bulk
    response. This is a SEPARATE action from the plain `act=vs` listing (which never includes
    a trustworthy running/stopped/suspended signal -- see parse_listvs_response's docstring)
    but reuses the SAME `vs` action name with an added query parameter, confirmed empirically
    on ir1 to return, in a single call regardless of fleet size, a top-level `status` dict
    keyed by vpsid (string) with per-VPS entries containing (among other fields) `status`
    (the documented 0/1/2 power-state code) and `network_status` (either `False`, meaning no
    network-level action is in effect, or a dict like `{"id": ..., "reason": ..., "date": ...}`
    describing WHY the VPS's networking was suspended, e.g. a bandwidth overage -- distinct
    from VM power state and never merged into it).

    Returns {"status_by_vpsid": {vpsid: {"runtime_status": ..., "network_suspended_reason":
    ...}}, "error": str|None}. Never raises; a malformed/missing response yields an empty map
    with an error string, never a fabricated status for any vpsid."""
    if not isinstance(parsed, dict):
        return {"status_by_vpsid": {}, "error": "top-level response was not a JSON object"}
    status_raw = parsed.get("status")
    if not isinstance(status_raw, dict):
        return {"status_by_vpsid": {}, "error": "response had no 'status' object"}

    out = {}
    for vpsid_key, entry in status_raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            vpsid = int(vpsid_key)
        except (TypeError, ValueError):
            continue
        runtime_status = map_runtime_status_code(entry.get("status"))
        network_status = entry.get("network_status")
        network_suspended_reason = (
            network_status.get("reason", UNAVAILABLE)
            if isinstance(network_status, dict) else None
        )
        out[vpsid] = {
            "runtime_status": runtime_status,
            "network_suspended_reason": network_suspended_reason,
        }
    return {"status_by_vpsid": out, "error": None}


def fetch_vs_status_bulk(creds, vpsids, call_vs_api=None, timeout: int = 20) -> dict:
    """Fetches runtime status for ALL given vpsids in exactly ONE API call (never one request
    per VPS) via the dedicated `act=vs&vs_status=<ids>` action. Returns parse_vs_status_
    response's shape. If `vpsids` is empty, returns an empty map without making any request.
    Any transport/parse failure is reported as a non-fatal error string -- callers must treat
    a failure here as "runtime status unavailable this cycle", never as "assume running"."""
    if not vpsids:
        return {"status_by_vpsid": {}, "error": None}
    if call_vs_api is None:
        import writer as writer_mod
        call_vs_api = writer_mod.call_vs_api
    ids_param = ",".join(str(v) for v in vpsids)
    try:
        result = call_vs_api(creds, timeout=timeout, extra_params={"vs_status": ids_param})
    except Exception as e:  # noqa: BLE001 -- a failed status enrichment must not break inventory sync
        return {"status_by_vpsid": {}, "error": str(e)}
    parsed = result.get("parsed")
    if parsed is None:
        return {"status_by_vpsid": {}, "error": "response was not valid JSON"}
    return parse_vs_status_response(parsed)


DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 20
DEFAULT_PAGE_RATE_LIMIT_SECONDS = 0.3


def fetch_vs_inventory(creds, call_vs_api=None, page_size: int = DEFAULT_PAGE_SIZE,
                        max_pages: int = DEFAULT_MAX_PAGES,
                        rate_limit_seconds: float = DEFAULT_PAGE_RATE_LIMIT_SECONDS,
                        sleep_fn=None) -> dict:
    """Walks ALL pages of `act=vs` (confirmed empirically in v1.5.0 development: this
    endpoint silently truncates to `page_size` records per call with no indicator in a
    single response -- the real ir1 installation returned exactly 50 of 60 real VPSs on a
    bare call before this fix). Stops when a page returns fewer than `page_size` records (the
    natural last page) or an empty page, bounded by `max_pages` so a misbehaving API can
    never loop forever. Rate-limited between page requests (never a tight loop hammering the
    API) -- entirely separate from the renewal engine's own rate limiting.

    Cross-checks the combined, deduplicated record count against the AUTHORITATIVE
    `servers[].numvps` count (a server-level metadata field, not a VPS listing itself) found
    in any one of the paginated responses. If the two disagree after exhausting pages, the
    result is marked incomplete with an explicit INCOMPLETE INVENTORY diagnostic -- never
    silently reported as fresh/complete, and never treated as evidence that the missing VPSs
    were deleted (inventory.py's ingest_snapshot already refuses an incomplete snapshot as a
    diff baseline; this is the second half of that guarantee, applied at the fetch layer).

    Returns the same shape as parse_listvs_response, plus: `authoritative_total` (int or
    None if never found), `fetched_count`, `pages_fetched`.
    """
    if call_vs_api is None:
        import writer as writer_mod
        call_vs_api = writer_mod.call_vs_api
    if sleep_fn is None:
        import time
        sleep_fn = time.sleep

    combined_vs = {}
    authoritative_total = None
    pages_fetched = 0
    fetch_error = None

    for page in range(1, max_pages + 1):
        try:
            result = call_vs_api(creds, extra_params={"reslen": page_size, "page": page})
        except Exception:
            # A failure on the very FIRST page (zero data fetched so far) means the API is
            # unreachable/broken entirely -- that's a hard ERROR for the caller to retry,
            # not a "some VPSs missing" incompleteness. A failure on a LATER page (some
            # pages already succeeded) is downgraded to an incompleteness reason instead,
            # since partial data was obtained and should still be reported as such rather
            # than discarded.
            if not combined_vs:
                raise
            fetch_error = f"page {page}: request failed mid-pagination"
            break
        parsed = result.get("parsed")
        if parsed is None:
            fetch_error = f"page {page}: response was not valid JSON"
            break
        if not isinstance(parsed, dict):
            fetch_error = f"page {page}: top-level response was not a JSON object"
            break

        if authoritative_total is None:
            servers = parsed.get("servers")
            if isinstance(servers, list) and servers and isinstance(servers[0], dict):
                try:
                    authoritative_total = int(servers[0].get("numvps"))
                except (TypeError, ValueError):
                    pass

        vs = parsed.get("vs")
        if not isinstance(vs, dict) or not vs:
            pages_fetched += 1
            break

        combined_vs.update(vs)
        pages_fetched += 1
        if len(vs) < page_size:
            break  # natural last page
        sleep_fn(rate_limit_seconds)
    else:
        fetch_error = fetch_error or f"stopped after max_pages={max_pages} without an empty page"

    parse_result = parse_listvs_response({"vs": combined_vs})
    fetched_count = len(parse_result["records"])

    # RUNTIME STATUS ENRICHMENT (v1.7.2): exactly ONE additional bulk call regardless of
    # fleet size (never one request per VPS -- see fetch_vs_status_bulk's docstring),
    # performed as part of this SAME inventory-sync workflow so no new timer/cadence is
    # introduced. A failure here never fails the whole sync -- it just leaves
    # runtime_status as "unavailable" (already the per-record default), matching the
    # explicit instruction to show "Unavailable" rather than fabricate a running state.
    vpsids_for_status = [r["vpsid"] for r in parse_result["records"]]
    status_result = fetch_vs_status_bulk(creds, vpsids_for_status, call_vs_api=call_vs_api)
    status_by_vpsid = status_result.get("status_by_vpsid") or {}
    for record in parse_result["records"]:
        enrichment = status_by_vpsid.get(record["vpsid"])
        if enrichment:
            record["runtime_status"] = enrichment["runtime_status"]
            record["network_suspended_reason"] = enrichment["network_suspended_reason"]
    parse_result["runtime_status_error"] = status_result.get("error")

    if fetch_error:
        parse_result["complete"] = False
        parse_result["error"] = fetch_error
    elif authoritative_total is not None and fetched_count != authoritative_total:
        parse_result["complete"] = False
        parse_result["error"] = (
            f"INCOMPLETE INVENTORY: fetched {fetched_count} of {authoritative_total} "
            f"VPSs reported by server metadata across {pages_fetched} page(s)"
        )

    parse_result["authoritative_total"] = authoritative_total
    parse_result["fetched_count"] = fetched_count
    parse_result["pages_fetched"] = pages_fetched
    return parse_result
