"""TixoCloud Control Center Telegram bot (v1.8.0 Part 3) -- command dispatch, authorization,
and rate limiting. Built entirely on the already-implemented, already-tested layers:
`control_center_api.py` for data (never re-derives business logic), `security.py` for
authorization/rate-limiting, `telegram_config.py` for settings/secrets, and
`telegram_transport.py` for actually talking to Telegram.

SCOPE OF THIS MODULE: the pure, network-independent dispatch logic -- `process_update()`
takes a Telegram "update" dict (the same shape `getUpdates`/a webhook would deliver) and
returns the reply text, entirely testable with fake update dicts and fixture databases,
never a real network call. The actual long-polling loop (repeatedly calling `getUpdates`
and feeding results to `process_update`) is NOT implemented here -- see
docs/DEVELOPMENT_PROGRESS.md for why, and for the exact next step.

AUTHORIZATION: every command (except /start and /help) requires the sender's numeric
Telegram user ID to be in `settings.admin_ids` or `settings.viewer_ids`
(`security.is_authorized_telegram_id` -- never username-based). Mutating actions (there are
none yet in this module -- see below) would additionally require admin, never viewer, but
every command currently implemented is READ-ONLY, so admin/viewer are treated identically
for now; the distinction exists in the data model for when a mutating command (e.g.
approving an emergency grant) is added.

RATE LIMITING: every update is checked against `security.check_rate_limit` BEFORE any
command logic runs (bucket = f"telegram:{user_id}") -- an unauthorized or spamming user
cannot cause unbounded backend work merely by sending many messages.
"""
from __future__ import annotations

import os
from decimal import Decimal

DEFAULT_RATE_LIMIT_MAX_EVENTS = 20
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60

HELP_TEXT = (
    "TixoCloud Control Center bot.\n\n"
    "/start - welcome message\n"
    "/help - this message\n"
    "/status - brief fleet/host/renewal status\n"
    "/host - host CPU/RAM/disk/temperature\n"
    "/network - current network interface + live/persistent traffic\n"
    "/traffic - historical traffic analytics (today/week/month)\n"
    "/vps - VPS inventory summary\n"
    "/renewals - renewal ledger summary\n"
    "/alerts - pending manual-review items and proposed emergency grants\n"
    "/reports - scheduled report status\n"
    "/backup - backup status\n"
    "/settings - current bot settings (never shows secrets)"
)

START_TEXT = (
    "Welcome to the TixoCloud Control Center bot. This bot reports read-only status from "
    "your Tixo Renewal installation. Send /help for the list of commands."
)


def build_transport(settings, secrets):
    """Builds the configured transport. Raises ValueError (never silently falls back to a
    different, more permissive mode) if the chosen mode is missing required configuration --
    per the explicit instruction to never silently fall back to an insecure mode."""
    import telegram_config as tc
    import telegram_transport as tt

    if settings.connectivity_mode == tc.MODE_DIRECT:
        return tt.DirectTransport(bot_token=secrets.bot_token)
    if settings.connectivity_mode == tc.MODE_SOCKS5:
        if not settings.socks5_host or not settings.socks5_port:
            raise ValueError("SOCKS5 mode selected but socks5_host/socks5_port are not configured")
        proxy = tt.Socks5ProxyConfig(host=settings.socks5_host, port=settings.socks5_port,
                                      username=settings.socks5_username,
                                      password=secrets.socks5_password)
        return tt.Socks5Transport(bot_token=secrets.bot_token, proxy=proxy)
    if settings.connectivity_mode == tc.MODE_RELAY:
        raise ValueError("RELAY mode requires a separately deployed relay worker -- see "
                          "docs/DEVELOPMENT_PROGRESS.md; this installation's direct transport "
                          "object is intentionally not constructed for this mode")
    raise ValueError(f"unknown connectivity mode: {settings.connectivity_mode!r}")


def test_transport_connectivity(transport) -> dict:
    """Generic connectivity check (calls Telegram's getMe) usable for ANY transport object
    (Direct or SOCKS5 alike) -- never raises, always returns {"ok": bool, "detail": ...} so
    a settings/installer screen can show a clean pass/fail without crashing."""
    import telegram_transport as tt
    try:
        result = transport.call("getMe")
        return {"ok": bool(result.get("ok")), "detail": result}
    except tt.TelegramTransportError as e:
        return {"ok": False, "detail": str(e)}


def _extract_command_and_sender(update: dict):
    """Returns (command, user_id, chat_id) or (None, None, None) if `update` isn't a
    recognizable text-message update (e.g. an edited_message, a non-command text, a
    callback_query not yet supported) -- never raises on an unexpected update shape."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None, None, None
    text = message.get("text")
    if not isinstance(text, str) or not text.startswith("/"):
        return None, None, None
    command = text.split()[0].split("@")[0].lower()  # strip args and @botname suffix
    user = message.get("from", {})
    user_id = user.get("id")
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    return command, user_id, chat_id


def _format_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "unavailable"
    for unit, divisor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= divisor:
            return f"{n / divisor:.2f} {unit}"
    return f"{n:.0f} B"


def _handle_status(config_dir, state_dir) -> str:
    import control_center_api as api
    snap = api.get_full_snapshot(config_dir, state_dir)
    renewal = snap["renewal"]
    fleet = snap["inventory"]["fleet"]
    net = snap["network"]
    lines = [
        "TixoCloud Control Center -- status",
        f"Write approved: {renewal.get('write_approved')}",
        f"Auto-renewal enabled: {renewal.get('auto_renewal_enabled')}",
        f"VPS total: {fleet.get('total_vps')} (running {fleet.get('running_count')}, "
        f"stopped {fleet.get('stopped_count')}, suspended {fleet.get('suspended_count')}, "
        f"unknown {fleet.get('unknown_status_count')})",
    ]
    if net.get("configured"):
        lines.append(f"Network interface: {net['interface']}")
    else:
        lines.append("Network interface: not configured")
    return "\n".join(lines)


def _handle_host(config_dir, state_dir) -> str:
    import control_center_api as api
    host = api.get_host_snapshot()
    lines = ["Host status"]
    if "cpu" in host:
        lines.append(f"CPU: {host['cpu']}")
    if "memory" in host:
        lines.append(f"Memory: {host['memory']}")
    if "uptime" in host:
        lines.append(f"Uptime: {host['uptime']}")
    return "\n".join(lines) if len(lines) > 1 else "Host status unavailable."


def _handle_network(config_dir, state_dir) -> str:
    import control_center_api as api
    net = api.get_network_overview(config_dir, state_dir)
    if not net["configured"]:
        return "No network interface configured yet."
    lines = [f"Network -- {net['interface']}"]
    cum = net.get("cumulative")
    if cum:
        rx, tx = cum["cumulative_rx_bytes"], cum["cumulative_tx_bytes"]
        lines.append(f"Download (RX) total: {_format_bytes(rx)}")
        lines.append(f"Upload (TX) total: {_format_bytes(tx)}")
        lines.append(f"Total traffic: {_format_bytes(rx + tx)}")
    rate = net.get("persistent_rate_bytes_per_sec")
    if rate:
        lines.append(f"Recent rate: RX {_format_bytes(rate['rx_bytes_per_sec'])}/s, "
                      f"TX {_format_bytes(rate['tx_bytes_per_sec'])}/s")
    return "\n".join(lines)


def _handle_traffic(config_dir, state_dir) -> str:
    import control_center_api as api
    net = api.get_network_overview(config_dir, state_dir)
    if not net["configured"]:
        return "No network interface configured yet."
    bundle = net["traffic_analytics"]
    lines = [f"Traffic analytics -- {net['interface']}"]
    for key, label in (("today", "Today"), ("current_week", "This week"),
                        ("current_gregorian_month", "This month")):
        period = bundle.get(key)
        if not period:
            continue
        if period["coverage_status"] == "NO_DATA":
            lines.append(f"{label}: no data yet")
        else:
            lines.append(f"{label}: {_format_bytes(period['combined_bytes'])} combined")
    return "\n".join(lines)


def _handle_vps(config_dir, state_dir) -> str:
    import control_center_api as api
    overview = api.get_vps_inventory_overview(state_dir)
    fleet = overview["fleet"]
    if fleet["total_vps"] == 0:
        return "No VPS inventory synced yet."
    lines = [
        f"VPS inventory: {fleet['total_vps']} total",
        f"Running: {fleet['running_count']}  Stopped: {fleet['stopped_count']}  "
        f"Suspended: {fleet['suspended_count']}  Unknown: {fleet['unknown_status_count']}",
        f"Limited-quota used: {_format_bytes(float(fleet['limited_used_sum_gb']) * 1e9)} "
        f"of {_format_bytes(float(fleet['limited_quota_sum_gb']) * 1e9)}",
    ]
    return "\n".join(lines)


def _handle_renewals(config_dir, state_dir) -> str:
    import control_center_api as api
    overview = api.get_renewal_overview(config_dir, state_dir)
    lines = [
        f"Write approved: {overview.get('write_approved')}",
        f"Auto-renewal enabled: {overview.get('auto_renewal_enabled')}",
        f"Monthly grant: {overview.get('monthly_grant_gb')} GB",
    ]
    per_node = overview.get("per_node", {})
    for node_id, info in per_node.items():
        if not info.get("initialized"):
            lines.append(f"Node {node_id}: not initialized")
            continue
        lines.append(f"Node {node_id}: {info['operations_total']} operations, "
                      f"{info['operations_unresolved']} unresolved")
    return "\n".join(lines)


def _handle_alerts(config_dir, state_dir) -> str:
    import control_center_api as api
    overview = api.get_emergency_policy_overview(config_dir, state_dir)
    lines = [f"Exhausted-bandwidth policy mode: {overview['mode']}"]
    pending = overview["pending_reviews"]
    proposed = overview["proposed_grants"]
    if pending:
        lines.append(f"Pending manual reviews: {len(pending)}")
        for item in pending[:5]:
            lines.append(f"  VPS {item['vpsid']}: {item['reason']}")
    else:
        lines.append("No pending manual reviews.")
    if proposed:
        lines.append(f"Proposed emergency grants: {len(proposed)}")
        for item in proposed[:5]:
            lines.append(f"  VPS {item['vpsid']}: +{item['amount_gb']} GB")
    else:
        lines.append("No proposed emergency grants.")
    return "\n".join(lines)


def _handle_reports(config_dir, state_dir, settings) -> str:
    return (f"Scheduled reports are not yet implemented (see docs/DEVELOPMENT_PROGRESS.md). "
            f"Configured schedule (not yet active): "
            f"{settings.report_hour:02d}:{settings.report_minute:02d} {settings.report_timezone}.")


def _handle_backup(config_dir, state_dir) -> str:
    return "Scheduled/encrypted backup delivery via Telegram is not yet implemented."


def _handle_settings(settings) -> str:
    return (f"Connectivity mode: {settings.connectivity_mode}\n"
            f"Enabled: {settings.enabled}\n"
            f"Admins: {len(settings.admin_ids)}  Viewers: {len(settings.viewer_ids)}\n"
            f"Report schedule: {settings.report_hour:02d}:{settings.report_minute:02d} "
            f"{settings.report_timezone}")


COMMANDS = ("/start", "/help", "/status", "/host", "/network", "/traffic", "/vps",
            "/renewals", "/alerts", "/reports", "/backup", "/settings")


def process_update(update: dict, settings, config_dir: str, state_dir: str,
                    rate_limit_conn=None, now=None) -> dict:
    """The core, network-independent dispatch function. Returns {"reply": str|None,
    "chat_id": int|None, "authorized": bool, "rate_limited": bool, "command": str|None} --
    callers (the polling loop, a webhook handler, or a test) decide what to do with `reply`
    (e.g. actually send it via a transport). Never raises on a malformed update -- returns
    reply=None in that case so the caller can simply skip it."""
    import security as sec

    command, user_id, chat_id = _extract_command_and_sender(update)
    if command is None:
        return {"reply": None, "chat_id": None, "authorized": False, "rate_limited": False,
                "command": None}

    if rate_limit_conn is not None:
        rl = sec.check_rate_limit(rate_limit_conn, f"telegram:{user_id}",
                                   max_events=DEFAULT_RATE_LIMIT_MAX_EVENTS,
                                   window_seconds=DEFAULT_RATE_LIMIT_WINDOW_SECONDS, now=now)
        if not rl["allowed"]:
            return {"reply": None, "chat_id": chat_id, "authorized": None,
                     "rate_limited": True, "command": command}

    if command in ("/start", "/help"):
        reply = START_TEXT if command == "/start" else HELP_TEXT
        return {"reply": reply, "chat_id": chat_id, "authorized": True, "rate_limited": False,
                "command": command}

    allowed_ids = list(settings.admin_ids) + list(settings.viewer_ids)
    if not sec.is_authorized_telegram_id(user_id, allowed_ids):
        return {"reply": "You are not authorized to use this bot.", "chat_id": chat_id,
                "authorized": False, "rate_limited": False, "command": command}

    handlers = {
        "/status": lambda: _handle_status(config_dir, state_dir),
        "/host": lambda: _handle_host(config_dir, state_dir),
        "/network": lambda: _handle_network(config_dir, state_dir),
        "/traffic": lambda: _handle_traffic(config_dir, state_dir),
        "/vps": lambda: _handle_vps(config_dir, state_dir),
        "/renewals": lambda: _handle_renewals(config_dir, state_dir),
        "/alerts": lambda: _handle_alerts(config_dir, state_dir),
        "/reports": lambda: _handle_reports(config_dir, state_dir, settings),
        "/backup": lambda: _handle_backup(config_dir, state_dir),
        "/settings": lambda: _handle_settings(settings),
    }
    handler = handlers.get(command)
    if handler is None:
        return {"reply": f"Unknown command: {command}. Send /help for the list.",
                "chat_id": chat_id, "authorized": True, "rate_limited": False, "command": command}

    try:
        reply = handler()
    except Exception as e:  # noqa: BLE001 -- a bot must never crash the dispatch loop on one bad update
        reply = f"Error handling {command}: {e}"
    return {"reply": reply, "chat_id": chat_id, "authorized": True, "rate_limited": False,
            "command": command}
