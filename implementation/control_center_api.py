"""Shared, read-only backend service layer for TixoCloud Control Center (v1.8.0 Part 2).

RULE, per the mission brief: "CLI, Telegram, and Web MUST share one authoritative backend
and data model... Avoid three independent implementations of business logic." Any future
Telegram bot command or Web dashboard endpoint that wants monitoring/status data calls INTO
this module -- it never re-derives inventory/analytics/policy logic itself.

SAFETY, EXPLICIT: every function here is READ-ONLY toward every database it touches, and
NONE of them call the Virtualizor Admin API directly. They only ever read from the EXISTING,
already-synced local caches that the project's own independent timers populate (inventory.
sqlite3 via virt-renewal-inventory-sync.timer, hostnet.sqlite3 via virt-renewal-hostnet-sync.
timer, emergency_policy.sqlite3 via the (not-yet-timer-driven) exhausted-bandwidth check, and
the renewal ledger self.sqlite3). This guarantees that no amount of Web/Telegram/CLI request
volume can ever increase real Virtualizor API load -- that stays bounded by the existing
timers' configured cadence, exactly per the mission's "avoid excessive Virtualizor API calls"
instruction.

`get_live_network_reading()` is the one exception to "only reads existing caches": it reads
a live /sys/class/net kernel counter directly (via hostinfo's existing, already-tested
primitive) -- this is a local filesystem read, not a network/API call, and is exactly as
cheap as the menu.py terminal live-view already does per refresh tick.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def _state_db_path(state_dir: str, filename: str) -> str:
    return os.path.join(state_dir, filename)


def get_host_snapshot() -> dict:
    """CPU/RAM/disk/temperature/uptime/network-interface-list -- a pure passthrough to
    hostinfo.collect_host_info(), kept here only so every consumer imports ONE module for
    "give me host status" rather than reaching into hostinfo.py directly and risking a
    future divergence if that function's shape changes."""
    import hostinfo
    return hostinfo.collect_host_info()


def get_live_network_reading(interface: str) -> dict:
    """One instantaneous kernel-counter read for `interface` -- NOT a rate (that requires
    two sequential reads with elapsed time, which is the CALLER's job, exactly as menu.py's
    own live-view loop already does via menu.compute_live_speed). Never fabricates a value
    when the interface is unavailable -- `ok` is False and both byte counts are None."""
    import hostinfo
    iface_dir = Path("/sys/class/net") / interface
    rx = hostinfo._read_interface_counter(iface_dir, "rx_bytes")
    tx = hostinfo._read_interface_counter(iface_dir, "tx_bytes")
    ok = isinstance(rx, int) and isinstance(tx, int)
    return {"interface": interface, "ok": ok, "rx_bytes": rx if ok else None,
            "tx_bytes": tx if ok else None,
            "read_at": datetime.now(timezone.utc).isoformat()}


def get_network_overview(config_dir: str, state_dir: str, tz_name: str = None) -> dict:
    """Configured interface, persistent cumulative totals, the 5-minute-sampler-derived
    rate, and the full historical traffic_analytics bundle -- everything the Web dashboard's
    "Network Traffic"/"Traffic Analytics" pages and a future Telegram /traffic command both
    need, from ONE call. Returns {"configured": False} (no interface chosen yet) rather
    than raising, matching every existing CLI screen's handling of that same unconfigured
    state."""
    import hostnet
    import hostnet_sync
    import traffic_analytics

    settings = hostnet_sync.load_settings(config_dir)
    interface = settings.get("interface")
    if not interface:
        return {"configured": False, "interface": None}

    if tz_name is None:
        import menu as menu_mod
        tz_name = menu_mod.load_display_tz(config_dir)

    conn = hostnet.open_db(_state_db_path(state_dir, "hostnet.sqlite3"))
    try:
        cumulative = hostnet.get_cumulative(conn, interface)
        persistent_rate = hostnet.compute_live_throughput(conn, interface)
        recent = hostnet.get_recent_samples(conn, interface, limit=1)
        analytics_bundle = traffic_analytics.all_periods(conn, interface, tz_name=tz_name)
    finally:
        conn.close()

    return {
        "configured": True,
        "interface": interface,
        "display_timezone": tz_name,
        "cumulative": cumulative,
        "persistent_rate_bytes_per_sec": persistent_rate,
        "latest_sample": recent[-1] if recent else None,
        "traffic_analytics": analytics_bundle,
    }


def get_vps_inventory_overview(state_dir: str, node_id: str = "self",
                                usage_threshold_percent=None) -> dict:
    """Latest already-synced inventory snapshot + fleet analytics -- makes NO Virtualizor
    API call (inventory.latest_records reads only from inventory.sqlite3, populated solely
    by virt-renewal-inventory-sync.timer's own independent cadence)."""
    import inventory as inv
    import analytics
    from decimal import Decimal

    conn = inv.open_db(_state_db_path(state_dir, "inventory.sqlite3"))
    try:
        sync_info = inv.last_sync_info(conn, node_id)
        records = inv.latest_records(conn, node_id)
    finally:
        conn.close()

    threshold = Decimal(str(usage_threshold_percent)) if usage_threshold_percent is not None else None
    fleet = analytics.compute_bandwidth_analytics(records, usage_threshold_percent=threshold)
    return {"sync_info": sync_info, "records": records, "fleet": fleet}


def get_renewal_overview(config_dir: str, state_dir: str) -> dict:
    """Thin wrapper over menu.collect_dashboard_data -- reuses that EXISTING, already-
    tested aggregation (policy/WRITE_APPROVED/per-node ledger health/timers/inventory
    freshness) rather than re-deriving any of it. menu.py's dict shape is kept exactly as-is
    here (not reshaped) so a future consumer can rely on the same, single, already-
    documented contract menu.py's own tests already pin down."""
    import menu as menu_mod
    return menu_mod.collect_dashboard_data(config_dir, state_dir)


def get_emergency_policy_overview(config_dir: str, state_dir: str) -> dict:
    """Current exhausted-bandwidth policy mode plus pending manual-review items and
    proposed-but-not-yet-applied emergency grants -- read-only toward emergency_policy.
    sqlite3, never toward the Virtualizor API or any customer quota."""
    import emergency_policy as emp

    policy = emp.load_policy_settings(config_dir)
    conn = emp.open_db(_state_db_path(state_dir, "emergency_policy.sqlite3"))
    try:
        pending_reviews = emp.list_pending_reviews(conn)
        proposed_grants = emp.list_proposed_grants(conn)
    finally:
        conn.close()
    return {
        "mode": policy.mode,
        "emergency_grant_amount_gb": str(policy.emergency_grant_amount_gb),
        "cooldown_hours": policy.cooldown_hours,
        "max_grants_per_month": policy.max_grants_per_month,
        "emergency_grants_approved": emp.is_emergency_grants_approved(config_dir),
        "pending_reviews": pending_reviews,
        "proposed_grants": proposed_grants,
    }


def get_host_time_overview(config_dir: str, state_dir: str) -> dict:
    """Current server-local time, uptime, boot time/ID, monitoring activation/elapsed, last
    persistent sample, data coverage, and a clearly-separate raw kernel-counter reading --
    a thin pass-through to host_time.get_host_time_snapshot (never re-derived here). Uses
    the configured hostnet interface automatically, if any."""
    import host_time
    import hostnet_sync
    settings = hostnet_sync.load_settings(config_dir)
    interface = settings.get("interface")
    return host_time.get_host_time_snapshot(config_dir, state_dir, interface=interface)


def get_full_snapshot(config_dir: str, state_dir: str) -> dict:
    """Everything-in-one-call convenience bundle -- what a Web dashboard's main overview
    page or a Telegram /status command wants, bounded to a small, fixed number of
    underlying read-only DB queries (never one Virtualizor API call per widget)."""
    import menu as menu_mod
    tz_name = menu_mod.load_display_tz(config_dir)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "display_timezone": tz_name,
        "host": get_host_snapshot(),
        "host_time": get_host_time_overview(config_dir, state_dir),
        "network": get_network_overview(config_dir, state_dir, tz_name=tz_name),
        "inventory": get_vps_inventory_overview(state_dir),
        "renewal": get_renewal_overview(config_dir, state_dir),
        "emergency_policy": get_emergency_policy_overview(config_dir, state_dir),
    }
