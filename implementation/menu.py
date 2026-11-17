#!/usr/bin/env python3
"""Tixo Renewal interactive management menu.

Pure stdlib (no third-party TUI dependency) -- a numbered text menu with ANSI colors that
degrade gracefully (auto-disabled when not a real tty, e.g. piped output, NO_COLOR set, or a
dumb terminal) and a `--status` flag for noninteractive automation (just delegates to the
existing `status` subcommand's output).

Every action here either calls an EXISTING renewal_cli.py function directly (never
re-implements its safety logic) or shells out via a list-of-arguments subprocess call (NEVER
shell=True with interpolated user input, to rule out command injection through any
operator-typed value -- GB amounts, interval minutes, confirmation strings).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import analytics
import hostinfo
import inventory as inv
import inventory_sync as isync
import nodes as nodes_mod
import policy as pol
import renewal_cli
import shadow_monitor as sm
import virtualizor_info as vinfo
import hostnet
import hostnet_sync
import traffic_analytics
import emergency_policy as emp
import telegram_config as tgc
import web_config as wcfg

GITHUB_API_LATEST_RELEASE = "https://api.github.com/repos/TheTixoCloud/tixo-renewal/releases/latest"
DEFAULT_DISPLAY_TZ = "Asia/Tehran"

BANNER = r"""
 ████████╗██╗██╗  ██╗ ██████╗      ██████╗██╗      ██████╗ ██╗   ██╗██████╗
 ╚══██╔══╝██║╚██╗██╔╝██╔═══██╗    ██╔════╝██║     ██╔═══██╗██║   ██║██╔══██╗
    ██║   ██║ ╚███╔╝ ██║   ██║    ██║     ██║     ██║   ██║██║   ██║██║  ██║
    ██║   ██║ ██╔██╗ ██║   ██║    ██║     ██║     ██║   ██║██║   ██║██║  ██║
    ██║   ██║██╔╝ ██╗╚██████╔╝    ╚██████╗███████╗╚██████╔╝╚██████╔╝██████╔╝
    ╚═╝   ╚═╝╚═╝  ╚═╝ ╚═════╝      ╚═════╝╚══════╝ ╚═════╝  ╚═════╝ ╚═════╝
                      C O N T R O L   C E N T E R
"""
BANNER_FALLBACK = r"""
  ========================================
   T I X O C L O U D   CONTROL CENTER
  ========================================
"""

DEFAULT_DASHBOARD_REFRESH_MIN = isync.DEFAULT_REFRESH_INTERVAL_MIN
DEFAULT_USAGE_THRESHOLD_PERCENT = Decimal("90")


def dashboard_settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "dashboard-settings.json")


def load_dashboard_settings(config_dir: str) -> dict:
    path = dashboard_settings_path(config_dir)
    if not os.path.exists(path):
        return {"refresh_interval_minutes": DEFAULT_DASHBOARD_REFRESH_MIN,
                "usage_threshold_percent": str(DEFAULT_USAGE_THRESHOLD_PERCENT)}
    try:
        import json
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"refresh_interval_minutes": DEFAULT_DASHBOARD_REFRESH_MIN,
                "usage_threshold_percent": str(DEFAULT_USAGE_THRESHOLD_PERCENT)}
    data.setdefault("refresh_interval_minutes", DEFAULT_DASHBOARD_REFRESH_MIN)
    data.setdefault("usage_threshold_percent", str(DEFAULT_USAGE_THRESHOLD_PERCENT))
    return data


def validate_usage_threshold_percent(value) -> Decimal:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a valid number: {value!r}")
    if not (0 <= d <= 100):
        raise ValueError("usage threshold must be between 0 and 100")
    return d


def save_dashboard_settings(config_dir: str, refresh_interval_minutes=None,
                             usage_threshold_percent=None) -> None:
    current = load_dashboard_settings(config_dir)
    if refresh_interval_minutes is not None:
        current["refresh_interval_minutes"] = renewal_cli.validate_interval_minutes(
            refresh_interval_minutes, min_minutes=1, max_minutes=1440)
    if usage_threshold_percent is not None:
        current["usage_threshold_percent"] = str(validate_usage_threshold_percent(usage_threshold_percent))
    import json
    path = dashboard_settings_path(config_dir)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(current, indent=2) + "\n")
    os.replace(tmp, path)


# ---------- timezone DISPLAY only -- never touches the host's actual system timezone --------

def display_settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "display-settings.json")


def load_display_tz(config_dir: str) -> str:
    """Display-only setting, stored separately from policy.json (which is schema-validated
    business-rule config) -- never affects the host's actual timezone, never written to by
    anything except the menu's own timezone-configuration action."""
    import json
    path = display_settings_path(config_dir)
    if not os.path.exists(path):
        return DEFAULT_DISPLAY_TZ
    try:
        return json.loads(Path(path).read_text()).get("display_tz", DEFAULT_DISPLAY_TZ)
    except (ValueError, OSError):
        return DEFAULT_DISPLAY_TZ


def validate_timezone_name(name: str) -> str:
    """Raises ValueError for anything zoneinfo can't resolve -- never persists an invalid
    name. stdlib only (Python 3.9+'s zoneinfo), no third-party dependency."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"not a valid IANA timezone name: {name!r} ({e})")
    return name


def save_display_tz(config_dir: str, tz_name: str) -> None:
    validated = validate_timezone_name(tz_name)
    import json
    path = display_settings_path(config_dir)
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps({"display_tz": validated}, indent=2) + "\n")
    os.replace(tmp, path)


def format_local_time(iso_utc_str: str, tz_name: str = DEFAULT_DISPLAY_TZ) -> str:
    """Pure display conversion -- the stored timestamp (always UTC ISO-8601 throughout this
    project) is never altered, only how it's PRINTED. Falls back to the raw UTC string if the
    timezone name or the stored timestamp is malformed, rather than raising in a dashboard
    render (never let a display bug crash the menu).

    v1.7.0: when the configured display timezone is Asia/Tehran, dates are additionally shown
    using the Jalali (Persian solar) calendar alongside the Gregorian clock time -- the
    underlying wall-clock time (hour/minute/second, correctly DST/UTC-offset-adjusted via
    zoneinfo) is identical either way; only the CALENDAR used for the date portion differs.
    Any other timezone keeps the plain Gregorian format."""
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_utc_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(tz_name))
        if tz_name == "Asia/Tehran":
            import jalali
            jalali_date = jalali.format_jalali_date(local)
            return f"{jalali_date} {local.strftime('%H:%M:%S %Z')} (Jalali)"
        return f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    except Exception:
        return f"{iso_utc_str} (UTC)"


# ---------- terminal/color helpers (pure, testable) -----------------------------------------

def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return os.environ.get("TERM", "") not in ("", "dumb")


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def supports_unicode() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or ""
    return "utf" in encoding.lower()


def render_banner(style: "Style") -> str:
    banner = BANNER if supports_unicode() else BANNER_FALLBACK
    return f"{style.BOLD}{style.ACCENT}{banner}{style.RESET}"


def clear_screen() -> None:
    """ANSI clear + cursor-home. NEVER called in noninteractive mode (checked by every
    caller via is_interactive() first) -- a piped/redirected output stream must never have
    control codes dumped into it."""
    if is_interactive():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


class Style:
    """ANSI codes, all-empty when color is unsupported -- every caller just uses these
    constants unconditionally, so there is exactly one place that decides color on/off.

    v1.5.0 theme: RED is the TixoCloud brand accent (headings, banner, the active section
    title) on a conceptually dark-gray background terminal; GRAY is used for secondary/
    de-emphasized text (replacing the old default-white-everywhere look). GREEN/YELLOW/RED
    remain the status-indicator colors (good/caution/bad) -- unrelated to the brand accent
    despite sharing the RED code; a bad-status red and the brand red are visually identical
    on purpose (both signal "pay attention here")."""
    def __init__(self, enabled: bool):
        self.RESET = "\033[0m" if enabled else ""
        self.BOLD = "\033[1m" if enabled else ""
        self.GREEN = "\033[32m" if enabled else ""
        self.RED = "\033[31m" if enabled else ""
        self.BRIGHT_RED = "\033[91m" if enabled else ""
        self.YELLOW = "\033[33m" if enabled else ""
        self.CYAN = "\033[36m" if enabled else ""
        self.GRAY = "\033[90m" if enabled else ""
        self.DIM = "\033[2m" if enabled else ""
        # Brand accent used for headings/titles/banner throughout the redesigned UI.
        self.ACCENT = self.BRIGHT_RED


# ---------- pure validation helpers (unit-tested) -------------------------------------------

def parse_gift_gb(raw: str) -> Decimal:
    """Validates an operator-typed gift amount. Never silently defaults -- an explicit "0"
    must parse to Decimal(0), never be treated as 'not provided'."""
    raw = raw.strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a valid number: {raw!r}")
    if value < 0:
        raise ValueError("gift amount must be >= 0")
    return value


def mask_secret_for_display(secret: str) -> str:
    """Never shows a stored credential in full -- used only for a short confirmation
    display, never logged, never written to any file."""
    if not secret:
        return "(empty)"
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 4) + secret[-2:]


def format_remote_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_newer_version(latest: str, current: str) -> bool:
    """Semantic comparison, not just string inequality -- a published release can lag behind
    a local dev version (e.g. this exact repo before v1.1.0 was tagged), which must never be
    reported as 'update available'."""
    return _version_tuple(latest) > _version_tuple(current)


def check_for_update(current_version: str = renewal_cli.VERSION, timeout: int = 5) -> dict:
    """Read-only HTTPS GET against the public GitHub API -- no auth, no write, never
    executes anything it downloads. Returns a dict describing the result; never raises."""
    try:
        req = urllib.request.Request(GITHUB_API_LATEST_RELEASE,
                                      headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            data = json.loads(resp.read().decode())
        latest = format_remote_version(data.get("tag_name", ""))
        return {"ok": True, "current": current_version, "latest": latest,
                "update_available": latest != "" and is_newer_version(latest, current_version),
                "url": data.get("html_url", "")}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"ok": False, "current": current_version, "error": str(e)}


# ---------- status collection (read-only, pure data gathering) ------------------------------

def _systemctl(*args) -> str:
    """List-of-arguments subprocess call ONLY -- never shell=True, never string-interpolated
    unit names (even though unit names here are hardcoded constants, not operator input)."""
    try:
        result = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or result.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"(systemctl unavailable: {e})"


def collect_timer_status() -> dict:
    out = {}
    for unit in ("virt-renewal-monitor.timer", "virt-renewal-apply.timer",
                 "virt-renewal-readiness-notifier.timer", "virt-renewal-inventory-sync.timer"):
        enabled = _systemctl("is-enabled", unit)
        active = _systemctl("is-active", unit)
        out[unit] = {"enabled": enabled, "active": active}
    return out


def classify_timer_health(active: str, enabled: str) -> str:
    """ACTIVE / INACTIVE / FAILED / UNKNOWN -- NEVER reports ACTIVE just because the unit
    file exists or is enabled; only systemctl's own reported `active` state counts."""
    if active == "active":
        return "ACTIVE"
    if active == "failed":
        return "FAILED"
    if active in ("inactive", "") and enabled in ("enabled", "disabled", "static"):
        return "INACTIVE"
    return "UNKNOWN"


def collect_dashboard_data(config_dir: str, state_dir: str) -> dict:
    """Pure read-only aggregation -- never writes anything. Returns a plain dict so the
    rendering function (render_dashboard) can be unit-tested without a real terminal."""
    data = {"write_approved": os.path.exists(os.path.join(config_dir, "WRITE_APPROVED"))}
    try:
        cfg = pol.load_policy_config(os.path.join(config_dir, "policy.json"))
        data["auto_renewal_enabled"] = cfg.global_defaults.auto_renewal_enabled
        data["monthly_grant_gb"] = str(cfg.global_defaults.monthly_grant_gb)
        data["policy_ok"] = True
    except Exception as e:
        data["policy_ok"] = False
        data["policy_error"] = str(e)
    try:
        nodes = nodes_mod.load_nodes(os.path.join(config_dir, "nodes.json"))
        data["nodes"] = [n.node_id for n in nodes]
        per_node = {}
        for n in nodes:
            db_path = n.db_path(state_dir)
            if not os.path.exists(db_path):
                per_node[n.node_id] = {"initialized": False}
                continue
            conn = sm.open_db(db_path)
            checkpoint = conn.execute(
                "SELECT last_poll_at FROM monitor_checkpoints WHERE id=1").fetchone()
            ops_total = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            ops_unresolved = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE status IN ('IN_PROGRESS','UNKNOWN_NEEDS_VERIFICATION')"
            ).fetchone()[0]
            deferred_rows = conn.execute(
                "SELECT vpsid, period_key, retry_count, created_at FROM operations "
                "WHERE status='DEFERRED_LOCKED' ORDER BY created_at DESC"
            ).fetchall()
            expired_rows = conn.execute(
                "SELECT vpsid, period_key FROM operations WHERE status='DEFERRED_EXPIRED_NEEDS_REVIEW'"
            ).fetchall()
            failed_rows = conn.execute(
                "SELECT vpsid, period_key, status FROM operations WHERE status IN "
                "('API_REPORTED_ERROR','UNKNOWN_NEEDS_VERIFICATION') ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            conn.close()
            per_node[n.node_id] = {
                "initialized": True,
                "last_poll_at": checkpoint[0] if checkpoint else None,
                "operations_total": ops_total,
                "operations_unresolved": ops_unresolved,
                "deferred": deferred_rows,
                "deferred_expired": expired_rows,
                "failed_or_uncertain": failed_rows,
            }
        data["per_node"] = per_node
    except Exception as e:
        data["nodes_error"] = str(e)
    data["timers"] = collect_timer_status()

    # v1.7.0: fleet inventory/analytics + auto-sync health, surfaced prominently on the
    # dashboard itself rather than requiring a separate screen visit.
    try:
        inv_conn = inv.open_db(_inventory_db_path())
        sync_info = inv.last_sync_info(inv_conn, "self")
        sync_state = isync.load_sync_state(state_dir)
        settings = load_dashboard_settings(config_dir)
        records = inv.latest_records(inv_conn, "self")
        freshness = isync.classify_freshness(
            sync_info["captured_at"] if sync_info else None, sync_state["last_attempt_status"],
            settings["refresh_interval_minutes"])
        threshold = Decimal(settings["usage_threshold_percent"])
        fleet = analytics.compute_bandwidth_analytics(records, usage_threshold_percent=threshold)
        data["inventory"] = {
            "sync_info": sync_info, "freshness": freshness,
            "last_attempt_status": sync_state["last_attempt_status"],
            "last_attempt_error": sync_state.get("last_error"),
            "refresh_interval_minutes": settings["refresh_interval_minutes"],
            "fleet": fleet,
        }
    except Exception as e:
        data["inventory_error"] = str(e)

    inv_timer = data["timers"].get("virt-renewal-inventory-sync.timer", {})
    data["inventory_sync_timer_health"] = classify_timer_health(
        inv_timer.get("active", ""), inv_timer.get("enabled", ""))
    return data


def render_dashboard(data: dict, style: Style, display_tz: str = "UTC") -> str:
    lines = [f"{style.BOLD}{style.ACCENT}=== Tixo Renewal Dashboard ==={style.RESET}"]
    if data.get("policy_ok"):
        gift = data["monthly_grant_gb"]
        auto = data["auto_renewal_enabled"]
        auto_color = style.GREEN if auto else style.YELLOW
        lines.append(f"  Monthly gift: {gift} GB")
        lines.append(f"  Auto-renewal: {auto_color}{'ENABLED' if auto else 'disabled'}{style.RESET}")
    else:
        lines.append(f"  {style.RED}Policy config error: {data.get('policy_error')}{style.RESET}")
    wa_color = style.GREEN if data.get("write_approved") else style.DIM
    lines.append(f"  WRITE_APPROVED: {wa_color}{'present' if data.get('write_approved') else 'absent (writes blocked)'}{style.RESET}")
    for node_id, info in data.get("per_node", {}).items():
        lines.append(f"\n  Node '{node_id}':")
        if not info.get("initialized"):
            lines.append(f"    {style.YELLOW}not initialized yet -- run install/dry-run first{style.RESET}")
            continue
        last_poll = info.get("last_poll_at")
        lines.append(f"    Last poll: {format_local_time(last_poll, display_tz) if last_poll else 'never'}")
        lines.append(f"    Operations: {info['operations_total']} total, "
                      f"{info['operations_unresolved']} unresolved")
        deferred = info.get("deferred") or []
        if deferred:
            lines.append(f"    {style.YELLOW}Deferred (backup/restore/migration lock, will "
                          f"retry automatically):{style.RESET}")
            for vpsid, period_key, retry_count, created_at in deferred:
                lines.append(f"      vpsid={vpsid} period={period_key} retries={retry_count} "
                              f"since={format_local_time(created_at, display_tz)}")
        expired = info.get("deferred_expired") or []
        if expired:
            lines.append(f"    {style.RED}Deferred past max age -- needs operator review:{style.RESET}")
            for vpsid, period_key in expired:
                lines.append(f"      vpsid={vpsid} period={period_key}")
        failed = info.get("failed_or_uncertain") or []
        if failed:
            lines.append(f"    {style.RED}Failed/uncertain (never auto-retried):{style.RESET}")
            for vpsid, period_key, status in failed[:5]:
                lines.append(f"      vpsid={vpsid} period={period_key} status={status}")
    lines.append(f"\n  {style.BOLD}Renewal timers:{style.RESET}")
    for unit, status in data.get("timers", {}).items():
        if unit == "virt-renewal-inventory-sync.timer":
            continue  # shown in its own Inventory/Auto-sync section below
        active = status["active"]
        color = style.GREEN if active == "active" else style.DIM
        lines.append(f"    {unit}: enabled={status['enabled']} {color}active={active}{style.RESET}")

    lines.append(f"\n  {style.BOLD}{style.ACCENT}Inventory & Auto-Sync:{style.RESET}")
    inv_data = data.get("inventory")
    if inv_data is None:
        lines.append(f"    {style.RED}unavailable: {data.get('inventory_error')}{style.RESET}")
    else:
        health = data["inventory_sync_timer_health"]
        health_color = {"ACTIVE": style.GREEN, "FAILED": style.RED,
                         "INACTIVE": style.YELLOW, "UNKNOWN": style.GRAY}[health]
        timer_cfg = data["timers"].get("virt-renewal-inventory-sync.timer", {})
        lines.append(f"    Auto-sync timer: {health_color}{health}{style.RESET} "
                     f"(enabled={timer_cfg.get('enabled', '?')})")
        lines.append(f"    Configured interval: {inv_data['refresh_interval_minutes']} min")
        sync_info = inv_data["sync_info"]
        if sync_info:
            lines.append(f"    Last successful sync: {format_local_time(sync_info['captured_at'], display_tz)}")
            total = sync_info.get("authoritative_total")
            count = sync_info["record_count"]
            complete_color = style.GREEN if (total is None or count == total) else style.RED
            lines.append(f"    Coverage: {complete_color}{count}/{total if total is not None else '?'}{style.RESET}  "
                         f"Freshness: {inv_data['freshness']}")
        else:
            lines.append(f"    {style.GRAY}No inventory sync has run yet.{style.RESET}")
        if inv_data["last_attempt_status"] not in (None, "OK"):
            lines.append(f"    {style.RED}Last attempt: {inv_data['last_attempt_status']} "
                         f"-- {inv_data.get('last_attempt_error')}{style.RESET}")
        fleet = inv_data["fleet"]
        lines.append(f"    Current VPS count: {fleet['total_vps']}  "
                     f"Limited: {fleet['limited_vps_count']}  Unlimited: {fleet['unlimited_vps_count']}")
        lines.append(f"    Allocated (limited): {fleet['limited_quota_sum_gb']} GB   "
                     f"Used (limited): {fleet['limited_used_sum_gb']} GB   "
                     f"Remaining: {fleet['limited_remaining_sum_gb']} GB")
        lines.append(f"    Used (unlimited): {fleet['unlimited_used_sum_gb']} GB   "
                     f"Used (entire fleet): {fleet['total_used_all_vps_gb']} GB   "
                     f"{style.GRAY}-- quota allocation, not sold/billed bandwidth{style.RESET}")
    return "\n".join(lines)


def render_progress_bar(percent, width: int = 20, style: Style = None) -> str:
    style = style or Style(enabled=False)
    if percent == hostinfo.UNAVAILABLE or percent is None:
        return "[" + "?" * width + "] n/a"
    try:
        pct = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        return "[" + "?" * width + "] n/a"
    filled = int(round(width * pct / 100))
    color = style.RED if pct >= 90 else (style.YELLOW if pct >= 70 else style.GREEN)
    bar = color + "#" * filled + style.RESET + "-" * (width - filled)
    return f"[{bar}] {pct:.1f}%"


def _format_bytes_human(num_bytes) -> str:
    """Decimal (SI) units -- GB/TB mean 10^9/10^12 bytes here, matching how storage/traffic
    is conventionally marketed and billed, NOT the binary GiB/TiB (2^30/2^40) a kernel
    sometimes reports. Documented explicitly since the two differ by ~7% at this scale."""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "unavailable"
    for unit, divisor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= divisor:
            return f"{n / divisor:.2f} {unit}"
    return f"{n:.0f} B"


_BIT_RATE_UNITS = (("Tbps", 1e12), ("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3))
_BYTE_RATE_UNITS = (("TB/s", 1e12), ("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3))


def _scale_rate_human(value: float, units: tuple, base_unit: str) -> str:
    """Shared decimal (1000-based) SI auto-scaling for a non-negative rate, used by both the
    bit-based and byte-based formatters below so they can never disagree on scaling logic.
    `units` must be ordered LARGEST divisor first.

    NO MISLEADING ROUNDING (v1.7.3): rounding `value/divisor` to 2 decimals can push it to
    exactly the next tier's threshold (e.g. 999999 B/s -> 999.999 KB/s -> rounds to "1000.00
    KB/s", which is wrong -- it should display as "1.00 MB/s"). Detected by checking whether
    the ROUNDED value would read >= 1000 and, if so, re-rendering against the next larger
    unit instead."""
    for idx, (unit, divisor) in enumerate(units):
        if value < divisor:
            continue
        scaled = value / divisor
        if round(scaled, 2) >= 1000 and idx > 0:
            bigger_unit, bigger_divisor = units[idx - 1]
            return f"{value / bigger_divisor:.2f} {bigger_unit}"
        return f"{scaled:.2f} {unit}"
    return f"{value:.0f} {base_unit}"


def _format_bits_per_sec_human(bytes_per_sec) -> str:
    try:
        bps = float(bytes_per_sec) * 8
    except (TypeError, ValueError):
        return "unavailable"
    return _scale_rate_human(bps, _BIT_RATE_UNITS, "bps")


def _format_bytes_per_sec_human(bytes_per_sec) -> str:
    """B/s -> KB/s -> MB/s -> GB/s -> TB/s, auto-scaled (decimal/SI, matching
    _format_bytes_human) -- used for the live hostnet screen's byte-based reading."""
    try:
        n = float(bytes_per_sec)
    except (TypeError, ValueError):
        return "unavailable"
    return _scale_rate_human(n, _BYTE_RATE_UNITS, "B/s")


def _format_dual_speed_human(bytes_per_sec) -> tuple:
    """Returns (bit_based_str, byte_based_str) for the SAME underlying bytes_per_sec reading
    (v1.7.3): bit-based is the networking-industry convention (ISP/NIC line rates), byte-
    based is the transfer/accounting convention (file sizes, quota consumption). Always
    derived from one measurement via the fixed 1 byte = 8 bits relationship -- the two
    strings can never describe different events or disagree about what happened."""
    return _format_bits_per_sec_human(bytes_per_sec), _format_bytes_per_sec_human(bytes_per_sec)


def _format_uptime_human(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _dedupe_temperature_labels(zones: list[dict]) -> list[str]:
    """Multiple thermal zones sharing the same generic 'type' (e.g. two zones both reporting
    a plain acpi label) previously rendered as confusing duplicate-looking entries. Appends
    a disambiguating index only when a type string actually repeats."""
    from collections import Counter
    type_counts = Counter(z["type"] for z in zones)
    seen = Counter()
    labels = []
    for z in zones:
        t = z["type"]
        if type_counts[t] > 1:
            seen[t] += 1
            labels.append(f"{t} #{seen[t]}={z['celsius']}°C")
        else:
            labels.append(f"{t}={z['celsius']}°C")
    return labels


def _section(style, title) -> str:
    return f"{style.BOLD}{style.ACCENT}{title}{style.RESET}"


def render_host_info(info: dict, style: Style, show_virtual_interfaces: bool = False) -> str:
    """Grouped into OS / CPU / Memory+swap / Storage / Network / Timezone -- each its own
    compact section rather than one wall of text. Virtual interfaces (one per running VM on
    a busy hypervisor -- previously flooding this screen with dozens of entries) are hidden
    by default; pass show_virtual_interfaces=True (wired to a dedicated menu action) to see
    them."""
    os_info = info["os"]
    cpu = info["cpu"]
    mem = info["memory"]
    usage = info["usage"]
    up = info["uptime"]
    temp = info["temperature"]

    lines = [_section(style, "OPERATING SYSTEM")]
    lines.append(f"  {os_info['hostname']}  --  {os_info['distribution']}")
    lines.append(f"  Kernel {os_info['kernel']} ({os_info['architecture']})")
    lines.append(f"  Uptime: {_format_uptime_human(up['uptime_seconds'])}   "
                 f"Load: {up['load_average'].get('1m')} / {up['load_average'].get('5m')} / "
                 f"{up['load_average'].get('15m')} (1m/5m/15m)")

    lines.append("")
    lines.append(_section(style, "CPU"))
    lines.append(f"  {cpu['vendor']} -- {cpu['model']}")
    lines.append(f"  {cpu['physical_cores']} physical cores, {cpu['logical_cpus']} logical "
                 f"CPUs, {cpu['threads_per_core']} threads/core")

    lines.append("")
    lines.append(_section(style, "MEMORY & SWAP"))
    lines.append(f"  RAM:  {mem['available_mb']} MB available of {mem['total_mb']} MB "
                 f"{render_progress_bar(usage['memory_percent'], style=style)}")
    lines.append(f"  Swap: {mem['swap_free_mb']} MB free of {mem['swap_total_mb']} MB")

    lines.append("")
    lines.append(_section(style, "STORAGE"))
    for d in info["disks"]:
        lines.append(f"  {d['mount_point']}: {d['used_gb']}/{d['total_gb']} GB used "
                     f"{render_progress_bar(d['percent_used'], style=style)}")

    lines.append("")
    lines.append(_section(style, "TEMPERATURE"))
    if temp["available"]:
        lines.append("  " + ", ".join(_dedupe_temperature_labels(temp["zones"])))
    else:
        lines.append(f"  {style.GRAY}unavailable ({temp['reason']}){style.RESET}")

    lines.append("")
    all_ifaces = info["network_interfaces"]
    # Default view: hide known-virtual (per-VM) interfaces AND interfaces that are neither
    # virtual nor carry any usable information (state/IP/counters all unavailable) -- an
    # unfamiliar-but-genuinely-informative interface is NEVER hidden just because its name
    # looks unusual, only because it has nothing to show.
    default_shown = [i for i in all_ifaces if not i.get("is_virtual") and not i.get("is_uninformative")]
    hidden_count = len(all_ifaces) - len(default_shown)
    lines.append(_section(style, "NETWORK"))
    shown = all_ifaces if show_virtual_interfaces else default_shown
    if not shown:
        lines.append(f"  {style.GRAY}(no interfaces to show){style.RESET}")
    for iface in shown:
        tag = f" {style.GRAY}[virtual]{style.RESET}" if iface.get("is_virtual") else ""
        rx, tx = iface.get("rx_bytes"), iface.get("tx_bytes")
        rx_str = f"Download (RX) {_format_bytes_human(rx)}" if isinstance(rx, int) else "Download (RX) unavailable"
        tx_str = f"Upload (TX) {_format_bytes_human(tx)}" if isinstance(tx, int) else "Upload (TX) unavailable"
        lines.append(f"  {iface['name']}: {iface['state']}, {iface['ipv4']} ({rx_str}, {tx_str}){tag}")
    if not show_virtual_interfaces and hidden_count:
        lines.append(f"  {style.GRAY}... {hidden_count} interface(s) hidden (virtual or "
                     f"uninformative) -- see 'Show all network interfaces (advanced)'{style.RESET}")

    lines.append("")
    lines.append(_section(style, "TIMEZONE"))
    lines.append(f"  Host system timezone: {info['host_timezone']}")

    host_time_snapshot = info.get("host_time_snapshot")
    if host_time_snapshot:
        lines.append("")
        lines.append(_section(style, "SERVER TIME & UPTIME"))
        lines.append(f"  Current local time: {host_time_snapshot['current_local']} "
                      f"({host_time_snapshot['display_timezone']})")
        lines.append(f"  Uptime: {host_time_snapshot['uptime_human']}")
        lines.append(f"  Last boot: {host_time_snapshot['boot_time_local']}")
        lines.append(f"  Boot ID: {host_time_snapshot['boot_id']}")
        if host_time_snapshot.get("network_configured"):
            lines.append(f"  Monitoring activation: {host_time_snapshot['monitoring_activation_local']}")
            lines.append(f"  Monitoring elapsed: {host_time_snapshot['monitoring_elapsed_human']}")
            lines.append(f"  Last persistent sample: {host_time_snapshot['last_persistent_sample_local']}")
            discontinuity = host_time_snapshot.get("data_coverage_discontinuity")
            gap = host_time_snapshot.get("data_coverage_gap_seconds")
            if discontinuity:
                lines.append(f"  {style.YELLOW}Data coverage: discontinuity ({discontinuity}) at "
                              f"last sample{style.RESET}")
            elif gap:
                lines.append(f"  Data coverage: OK (last gap {gap:.0f}s)")
    return "\n".join(lines)


def render_virtualizor_info(v_info: dict, style: Style) -> str:
    lines = [f"{style.BOLD}{style.ACCENT}=== Virtualizor ==={style.RESET}"]
    installed = v_info["installed"]
    color = style.GREEN if installed else style.RED
    lines.append(f"  Installed: {color}{installed}{style.RESET}  Version: {v_info['version']}")
    techs = v_info["virtualization_technologies"]
    lines.append(f"  Virtualization technologies detected: {', '.join(techs) if techs else '(none detected)'}")
    api = v_info.get("api")
    if api:
        if api["ok"]:
            lines.append(f"  API: {style.GREEN}reachable{style.RESET} ({api['latency_ms']} ms)")
        else:
            lines.append(f"  API: {style.RED}error{style.RESET} ({api['error']})")
    return "\n".join(lines)


def render_analytics_summary(result: dict, style: Style) -> str:
    lines = [f"{style.BOLD}{style.ACCENT}=== Bandwidth Analytics ==={style.RESET}"]
    lines.append(f"  Total VPS: {result['total_vps']}  Limited: {result['limited_vps_count']}  "
                 f"Unlimited: {result['unlimited_vps_count']}  Unknown quota: {result['unknown_quota_vps_count']}")
    lines.append(f"  Status: running={result['running_count']} stopped={result['stopped_count']} "
                 f"suspended={result['suspended_count']} unknown={result['unknown_status_count']}")
    lines.append(f"  Limited fleet: {result['limited_used_sum_gb']} / {result['limited_quota_sum_gb']} GB used "
                 f"(coverage {result['limited_used_sum_coverage']}), "
                 f"{result['limited_remaining_sum_gb']} GB remaining")
    lines.append(f"  Unlimited fleet actual usage: {result['unlimited_used_sum_gb']} GB "
                 f"(coverage {result['unlimited_used_sum_coverage']})")
    lines.append(f"  Entire fleet total USED: {result['total_used_all_vps_gb']} GB "
                 f"(coverage {result['total_used_all_vps_coverage']}) "
                 f"{style.GRAY}-- quota allocation, not sold/billed bandwidth{style.RESET}")
    if result.get("vps_near_threshold"):
        lines.append(f"  {style.YELLOW}Near usage threshold: {result['vps_near_threshold']}{style.RESET}")
    if result["vps_over_threshold"]:
        lines.append(f"  {style.RED}Over usage threshold: {result['vps_over_threshold']}{style.RESET}")
    if result.get("vps_over_limit"):
        lines.append(f"  {style.BOLD}{style.RED}Over 100% of quota: {result['vps_over_limit']}{style.RESET}")
    if result["top_consumers"]:
        lines.append("  Top consumers: " + ", ".join(
            f"vpsid={c['vpsid']} ({c['used_gb']} GB)" for c in result["top_consumers"][:5]))
    return "\n".join(lines)


def paginate(records: list, page: int = 1, page_size: int = 20):
    total_pages = max(1, (len(records) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return records[start:start + page_size], page, total_pages


def filter_and_sort_vps(records: list[dict], search: str = None, status_filter: str = None,
                         sort_by: str = "vpsid") -> list[dict]:
    out = records
    if search:
        s = search.lower()
        out = [r for r in out if s in str(r.get("vpsid", "")).lower()
               or s in str(r.get("hostname", "")).lower()
               or s in str(r.get("ip_primary", "")).lower()]
    if status_filter:
        out = [r for r in out if str(r.get("runtime_status", "")).lower() == status_filter.lower()]

    numeric_fields = ("vpsid", "quota_gb", "used_gb")
    def sort_key(r):
        value = r.get(sort_by)
        if sort_by in numeric_fields:
            try:
                return (0, Decimal(str(value)))
            except Exception:
                return (1, Decimal(0))  # unparseable values sort after valid numeric ones
        return (0, str(value or ""))
    try:
        out = sorted(out, key=sort_key)
    except TypeError:
        pass
    return out


def _status_color(status, style: Style) -> str:
    s = str(status).lower()
    if s == "suspended":
        return style.RED
    if s == "running":
        return style.GREEN
    if s == "stopped":
        return style.YELLOW
    return style.GRAY  # "unavailable"/"unknown" and anything unrecognized


def render_vps_table(records: list[dict], style: Style) -> str:
    """VPS ID | Hostname | Primary IP | Status | Quota | Used | Usage% -- column widths
    adapt to the terminal width (narrower hostname/IP columns on a narrow terminal rather
    than wrapping/corrupting the table)."""
    if not records:
        return f"  {style.GRAY}(no VPS records -- run an inventory sync first){style.RESET}"
    try:
        import shutil
        term_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    except Exception:
        term_width = 100
    narrow = term_width < 100
    host_w, ip_w = (16, 13) if narrow else (22, 15)

    header = (f"  {'VPSID':>7}  {'HOSTNAME':<{host_w}}  {'IP':<{ip_w}}  {'STATUS':<10}  "
              f"{'QUOTA_GB':>9}  {'USED_GB':>9}  {'USAGE':<14}")
    lines = [style.BOLD + style.ACCENT + header + style.RESET]
    for r in records:
        quota, used = r.get("quota_gb"), r.get("used_gb")
        try:
            is_unlimited = Decimal(str(quota)) <= 0
        except Exception:
            is_unlimited = False
        try:
            pct = (Decimal(str(used)) / Decimal(str(quota))) * 100 if not is_unlimited else None
        except Exception:
            pct = None
        usage_display = "UNLIMITED" if is_unlimited else render_progress_bar(
            float(pct) if pct is not None else None, width=10, style=style)
        runtime_status = r.get("runtime_status", "unavailable")
        color = _status_color(runtime_status, style)
        ip = str(r.get("ip_primary", "unavailable"))
        lines.append(f"  {r['vpsid']:>7}  {str(r.get('hostname'))[:host_w]:<{host_w}}  "
                     f"{ip[:ip_w]:<{ip_w}}  "
                     f"{color}{str(runtime_status)[:10]:<10}{style.RESET}  "
                     f"{str(quota):>9}  {str(used):>9}  {usage_display}")
    return "\n".join(lines)


def render_vps_detail(record: dict, style: Style) -> str:
    """STATUS FIELDS ARE DELIBERATELY SPLIT (v1.7.2) into three distinct concepts, per the
    real-world confusion this is designed to avoid: Runtime Status (is the VM actually
    powered on right now -- from virtualizor_info.fetch_vs_status_bulk's authoritative bulk
    call), Billing Suspended (an administrative flag, unrelated to power state -- a VM can
    be running while billing-suspended, or vice versa), and Network Suspended (Virtualizor
    may cut network access for a specific reason, e.g. a bandwidth overage, while the VM
    process itself keeps running)."""
    lines = [f"{style.BOLD}{style.ACCENT}VPS {record['vpsid']} detail{style.RESET}"]
    runtime_status = record.get("runtime_status", "unavailable")
    color = _status_color(runtime_status, style)
    lines.append(f"  Hostname:       {record.get('hostname')}")
    lines.append(f"  Runtime Status: {color}{runtime_status}{style.RESET}")
    billing_suspended = "yes" if str(record.get("status")).lower() == "suspended" else "no"
    lines.append(f"  Billing Suspended: {billing_suspended}")
    network_reason = record.get("network_suspended_reason")
    if network_reason:
        lines.append(f"  {style.YELLOW}Network Suspended: yes (reason: {network_reason}){style.RESET}")
    else:
        lines.append(f"  Network Suspended: no")
    lines.append(f"  Quota:          {record.get('quota_gb')} GB")
    lines.append(f"  Used:           {record.get('used_gb')} GB")
    lines.append(f"  RAM:            {record.get('ram_mb')} MB")
    lines.append(f"  Cores:          {record.get('cores')}")
    lines.append(f"  Disk:           {record.get('disk_gb')} GB")
    lines.append(f"  Virtualization: {record.get('virtualization')}")
    lines.append(f"  Locked:         {record.get('locked')}")
    lines.append(f"  Node (serid):   {record.get('node_serid')}")
    lines.append(f"  Primary IP:     {record.get('ip_primary')}")
    lines.append(f"  IP count:       {record.get('ip_count')}")
    ip_list = record.get("ip_list")
    if isinstance(ip_list, list) and ip_list:
        lines.append(f"  All assigned IPs: {', '.join(str(ip) for ip in ip_list)}")
    return "\n".join(lines)


def render_hostnet_panel(interface: str, cumulative: dict, live_rate: dict,
                          recent_sample: dict, style: Style, display_tz: str = "UTC",
                          kernel_counter: dict = None) -> str:
    """HOST NETWORK -- <interface> panel per the spec layout. `cumulative`/`recent_sample`
    may be None (never sampled yet); `live_rate` may be None (fewer than 2 samples, or the
    most recent pair spans a discontinuity) -- both cases rendered honestly, never
    fabricated as zero."""
    lines = [f"{style.BOLD}{style.ACCENT}HOST NETWORK -- {interface}{style.RESET}"]
    if cumulative is None:
        lines.append(f"  {style.GRAY}No samples recorded yet for this interface.{style.RESET}")
        return "\n".join(lines)

    rx_total = cumulative["cumulative_rx_bytes"]
    tx_total = cumulative["cumulative_tx_bytes"]
    lines.append(f"  Host Download (RX) Total: {_format_bytes_human(rx_total)}")
    lines.append(f"  Host Upload (TX) Total:   {_format_bytes_human(tx_total)}")
    lines.append(f"  Host Total Traffic:       {_format_bytes_human(rx_total + tx_total)}")
    lines.append("")
    if live_rate is None:
        lines.append(f"  Persistent Download (RX) Speed: {style.GRAY}unavailable{style.RESET}")
        lines.append(f"  Persistent Upload (TX) Speed:   {style.GRAY}unavailable{style.RESET}")
        lines.append(f"  Persistent Combined Speed:      {style.GRAY}unavailable{style.RESET}")
    else:
        rx_bps, tx_bps = live_rate["rx_bytes_per_sec"], live_rate["tx_bytes_per_sec"]
        combined_bps = rx_bps + tx_bps
        for label, value in (("Persistent Download (RX) Speed", rx_bps),
                              ("Persistent Upload (TX) Speed", tx_bps),
                              ("Persistent Combined Speed", combined_bps)):
            bit_str, byte_str = _format_dual_speed_human(value)
            lines.append(f"  {label}:")
            lines.append(f"      {style.BOLD}{bit_str}{style.RESET}")
            lines.append(f"      {byte_str}")
        lines.append(f"  {style.GRAY}(computed from the 5-minute persistent sampler, not live){style.RESET}")
    lines.append("")
    if recent_sample:
        lines.append(f"  Last Sample:        {format_local_time(recent_sample['sampled_at'], display_tz)}")
    if cumulative.get("first_sample_at"):
        lines.append(f"  Monitoring Coverage: since {format_local_time(cumulative['first_sample_at'], display_tz)}")
    if recent_sample and recent_sample.get("discontinuity_type") == hostnet.DISCONTINUITY_REBOOT:
        lines.append(f"  {style.YELLOW}Last Reboot:         detected at {format_local_time(recent_sample['sampled_at'], display_tz)}{style.RESET}")
    gap = recent_sample.get("gap_seconds") if recent_sample else None
    if recent_sample and recent_sample.get("discontinuity_type"):
        lines.append(f"  {style.YELLOW}Data Quality: discontinuity ({recent_sample['discontinuity_type']}) "
                     f"at last sample -- some traffic in this gap is not observable, never invented.{style.RESET}")
    elif gap and gap > 0:
        lines.append(f"  Data Quality: OK (last gap {gap:.0f}s)")

    if kernel_counter is not None:
        lines.append("")
        lines.append(f"  {style.BOLD}KERNEL COUNTER -- since last interface counter reset{style.RESET}")
        if kernel_counter.get("ok"):
            lines.append(f"    RX: {_format_bytes_human(kernel_counter['rx_bytes'])}  "
                          f"TX: {_format_bytes_human(kernel_counter['tx_bytes'])}  "
                          f"Combined: {_format_bytes_human(kernel_counter['combined_bytes'])}")
        else:
            lines.append(f"    {style.GRAY}unavailable{style.RESET}")
        lines.append(f"    Source: {kernel_counter.get('source', 'unavailable')}")
        lines.append(f"    {style.GRAY}{kernel_counter.get('reset_semantics', '')}{style.RESET}")

    lines.append(f"\n  {style.GRAY}Download (RX) = bytes received by this HOST's monitored interface. "
                 f"Upload (TX) = bytes transmitted. Total Traffic = Download + Upload. These are "
                 f"from the physical host's perspective, not necessarily any one VPS customer's "
                 f"perspective. GB/TB here are decimal (10^9/10^12 bytes).{style.RESET}")
    return "\n".join(lines)


def render_live_hostnet_panel(interface: str, live_speed: dict, live_counters_ok: bool,
                               cumulative: dict, recent_sample: dict, style: Style,
                               display_tz: str = "UTC") -> str:
    """The TRUE live screen (v1.7.2, Part 1 of the live-traffic fix): `live_speed` is derived
    from TWO SEQUENTIAL RAW READS of /sys/class/net/<interface>/statistics/{rx,tx}_bytes taken
    roughly a second apart by the caller's refresh loop -- NEVER from the persistent 5-minute
    hostnet sampler, and NEVER written back into hostnet's database (this function and its
    caller are read-only with respect to that persistent accounting; see action_view_hostnet).

    `live_counters_ok` is False when the monitored interface's kernel counters could not be
    read this tick (e.g. the interface was removed) -- in that case an explicit stale/
    unavailable warning is shown and no live speed is fabricated, even if a previous tick had
    a valid reading."""
    lines = [f"{style.BOLD}{style.ACCENT}LIVE HOST TRAFFIC -- {interface}{style.RESET}"]
    if not live_counters_ok:
        lines.append(f"  {style.RED}WARNING: live kernel counters for '{interface}' are "
                      f"currently unavailable -- showing no live speed rather than a stale "
                      f"or fabricated value.{style.RESET}")
    elif live_speed is None:
        lines.append(f"  {style.GRAY}Live Download (RX): measuring... (first sample this view){style.RESET}")
        lines.append(f"  {style.GRAY}Live Upload (TX):   measuring...{style.RESET}")
        lines.append(f"  {style.GRAY}Live Combined:      measuring...{style.RESET}")
    else:
        rx_bps, tx_bps = live_speed["rx_bytes_per_sec"], live_speed["tx_bytes_per_sec"]
        combined_bps = rx_bps + tx_bps
        for label, value in (("Live Download (RX)", rx_bps), ("Live Upload (TX)", tx_bps),
                              ("Live Combined", combined_bps)):
            bit_str, byte_str = _format_dual_speed_human(value)
            lines.append(f"  {label}:")
            lines.append(f"      {style.BOLD}{style.GREEN}{bit_str}{style.RESET}")
            lines.append(f"      {style.GREEN}{byte_str}{style.RESET}")
    lines.append(f"  {style.GRAY}(live speed refreshes ~1x/sec from this host's live kernel "
                 f"counters -- separate from, and never affecting, the 5-minute persistent "
                 f"sampler below){style.RESET}")
    lines.append("")

    if cumulative is None:
        lines.append(f"  {style.GRAY}No persistent samples recorded yet for this interface.{style.RESET}")
    else:
        rx_total = cumulative["cumulative_rx_bytes"]
        tx_total = cumulative["cumulative_tx_bytes"]
        lines.append(f"  Persistent Download (RX) Total: {_format_bytes_human(rx_total)}")
        lines.append(f"  Persistent Upload (TX) Total:   {_format_bytes_human(tx_total)}")
        lines.append(f"  Persistent Total Traffic:       {_format_bytes_human(rx_total + tx_total)}")
        if recent_sample:
            lines.append(f"  Latest Persistent Sample:       "
                          f"{format_local_time(recent_sample['sampled_at'], display_tz)}")
        if recent_sample and recent_sample.get("discontinuity_type"):
            lines.append(f"  {style.YELLOW}Monitoring Health: discontinuity "
                          f"({recent_sample['discontinuity_type']}) at last persistent sample{style.RESET}")
        else:
            lines.append(f"  Monitoring Health: OK")

    lines.append(f"\n  {style.GRAY}Download (RX) = bytes received by this HOST's monitored "
                 f"interface. Upload (TX) = bytes transmitted. From the physical host's "
                 f"perspective, not necessarily any one VPS customer's perspective.{style.RESET}")
    return "\n".join(lines)


class _RawTerminalGuard:
    """Context manager for responsive single-key input (Part 2): puts stdin into cbreak mode
    (keys read immediately, no Enter required, no local echo of control keys) and GUARANTEES
    the original terminal attributes are restored in a `finally`-equivalent `__exit__` on
    EVERY exit path -- normal return, `break`, or any exception including KeyboardInterrupt.
    Never used if `sys.stdin` isn't a real tty (see supports_raw_input())."""

    def __init__(self):
        self._fd = None
        self._old_settings = None

    def __enter__(self):
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._old_settings is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
        return False  # never swallow exceptions -- callers handle KeyboardInterrupt themselves


def supports_raw_input() -> bool:
    """False in any environment where cbreak-mode single-key input can't work (piped stdin,
    not a tty, termios unavailable) -- the live screen must fall back gracefully rather than
    raise, per the explicit 'graceful fallback' requirement."""
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def poll_single_key(timeout_seconds: float):
    """Non-blocking (bounded-wait) single-character read -- returns None if no key arrived
    within `timeout_seconds`, otherwise the one character read. Uses select() so the live
    refresh loop never blocks waiting for input the way a bare input() call would (that bare
    call is the confirmed root cause of the old hostnet screen never updating without
    Enter -- see action_view_hostnet)."""
    import select
    ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
    if not ready:
        return None
    return sys.stdin.read(1)


def compute_live_speed(prev_counters, rx, tx, now_mono):
    """Pure delta/sec calculation from two sequential raw counter reads: `prev_counters` is
    either None (no prior reading this view -- e.g. the very first tick) or (prev_rx, prev_tx,
    prev_mono). Returns None (never a fabricated rate) if there's no prior reading, no elapsed
    time, or either counter went backwards (a counter reset/interface replacement -- see
    hostnet.py's own discontinuity handling for the persistent-sample equivalent of this same
    rule: a decrease is never reported as negative throughput, and never silently absorbed into
    a positive one either)."""
    if prev_counters is None:
        return None
    prev_rx, prev_tx, prev_mono = prev_counters
    dt = now_mono - prev_mono
    if dt <= 0 or rx < prev_rx or tx < prev_tx:
        return None
    return {"rx_bytes_per_sec": (rx - prev_rx) / dt, "tx_bytes_per_sec": (tx - prev_tx) / dt}


def read_live_interface_counters(interface: str):
    """ONE raw read of /sys/class/net/<interface>/statistics/{rx,tx}_bytes -- the same
    primitive hostinfo.py already uses elsewhere, reused here (not reimplemented) so there is
    exactly one code path that touches these sysfs files. Returns (rx_bytes, tx_bytes) as ints,
    or (None, None) if either counter couldn't be read (interface removed, permissions, etc).
    Never writes anywhere; never touches hostnet's persistent database."""
    iface_dir = Path("/sys/class/net") / interface
    rx = hostinfo._read_interface_counter(iface_dir, "rx_bytes")
    tx = hostinfo._read_interface_counter(iface_dir, "tx_bytes")
    if isinstance(rx, int) and isinstance(tx, int):
        return rx, tx
    return None, None


# ---------- menu actions ---------------------------------------------------------------------

def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def action_install():
    renewal_cli.cmd_install(argparse.Namespace())


def action_dashboard(style: Style):
    data = collect_dashboard_data(renewal_cli.CONFIG_DIR, renewal_cli.STATE_DIR)
    tz = load_display_tz(renewal_cli.CONFIG_DIR)
    print(render_dashboard(data, style, display_tz=tz))


def action_configure_timezone():
    import host_time as ht
    current = load_display_tz(renewal_cli.CONFIG_DIR)
    suggested_host_tz = ht.detect_default_display_timezone()
    if suggested_host_tz != current:
        print(f"Detected host system timezone: {suggested_host_tz} (current display "
              f"setting: {current})")
    raw = input(f"Display timezone (IANA name, e.g. Asia/Tehran) [{current}]: ").strip() or current
    try:
        save_display_tz(renewal_cli.CONFIG_DIR, raw)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    print(f"Display timezone set to {raw}. This only affects how times are SHOWN in this "
          f"menu -- the host's actual system timezone is never changed.")

    current_pref = ht.load_calendar_preference(renewal_cli.CONFIG_DIR)
    pref_raw = input(f"Calendar preference [{current_pref}] "
                      f"({'/'.join(ht.VALID_CALENDAR_PREFERENCES)}): ").strip() or current_pref
    try:
        ht.save_calendar_preference(renewal_cli.CONFIG_DIR, pref_raw)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    print(f"Calendar preference set to {pref_raw}.")


def action_configure_gift():
    raw = input("Monthly renewal gift in GB (0 is valid): ")
    try:
        gift = parse_gift_gb(raw)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    path = os.path.join(renewal_cli.CONFIG_DIR, "policy.json")
    cfg = pol.load_policy_config(path)
    import json
    data = json.loads(Path(path).read_text())
    data["global_defaults"]["monthly_grant_gb"] = str(gift)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    print(f"Monthly gift set to {gift} GB.")


def action_configure_intervals():
    try:
        monitor_min = renewal_cli.validate_interval_minutes(
            input(f"Monitor interval in minutes [{renewal_cli.DEFAULT_MONITOR_INTERVAL_MIN}]: ")
            or renewal_cli.DEFAULT_MONITOR_INTERVAL_MIN)
        apply_min = renewal_cli.validate_interval_minutes(
            input(f"Apply interval in minutes [{renewal_cli.DEFAULT_APPLY_INTERVAL_MIN}]: ")
            or renewal_cli.DEFAULT_APPLY_INTERVAL_MIN)
        notifier_min = renewal_cli.validate_interval_minutes(
            input(f"Notifier interval in minutes [{renewal_cli.DEFAULT_NOTIFIER_INTERVAL_MIN}]: ")
            or renewal_cli.DEFAULT_NOTIFIER_INTERVAL_MIN)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    renewal_cli.write_systemd_units(monitor_min, apply_min, notifier_min)
    print(f"Regenerated systemd templates in {renewal_cli.SYSTEMD_OUT_DIR} "
          f"(monitor={monitor_min}min, apply={apply_min}min, notifier={notifier_min}min).")
    if os.geteuid() == 0 and _confirm("Install these into /etc/systemd/system and restart the timers now?"):
        units = ["virt-renewal-monitor.service", "virt-renewal-monitor.timer",
                  "virt-renewal-apply.service", "virt-renewal-apply.timer",
                  "virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer"]
        src_paths = [os.path.join(renewal_cli.SYSTEMD_OUT_DIR, u) for u in units
                     if os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, u))]
        subprocess.run(["cp"] + src_paths + ["/etc/systemd/system/"], check=False)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "restart", "virt-renewal-monitor.timer",
                         "virt-renewal-apply.timer", "virt-renewal-readiness-notifier.timer"],
                        check=False)
        print("Applied. Restarting a timer re-reads its schedule; no catch-up run occurs "
              "(Persistent=true is never set).")
    else:
        print("Not applied to /etc/systemd/system -- re-run as root and confirm to apply, "
              "or copy the files yourself.")


def action_toggle_auto_renewal(enable: bool):
    path = os.path.join(renewal_cli.CONFIG_DIR, "policy.json")
    import json
    data = json.loads(Path(path).read_text())
    data["global_defaults"]["auto_renewal_enabled"] = enable
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    print(f"auto_renewal_enabled set to {enable}.")
    if not enable:
        marker = renewal_cli.WRITE_APPROVAL_MARKER
        if os.path.exists(marker) and _confirm(f"Also remove {marker} (full write lock-out)?"):
            os.remove(marker)
            print("WRITE_APPROVED removed -- all writes now structurally blocked.")


def action_view_logs():
    print(_systemctl("status", "virt-renewal-apply.service", "--no-pager", "-n", "20"))
    for node in nodes_mod.load_nodes(os.path.join(renewal_cli.CONFIG_DIR, "nodes.json")):
        db_path = node.db_path(renewal_cli.STATE_DIR)
        if not os.path.exists(db_path):
            continue
        conn = sm.open_db(db_path)
        rows = conn.execute(
            "SELECT operation_id, vpsid, status, requested_new_quota_gb, verified_after_quota_gb "
            "FROM operations ORDER BY created_at DESC LIMIT 10").fetchall()
        conn.close()
        print(f"\nRecent operations for node '{node.node_id}':")
        for r in rows:
            print(f"  {r}")


def action_check_updates():
    result = check_for_update()
    if not result["ok"]:
        print(f"Could not check for updates: {result['error']}")
        return
    if result["update_available"]:
        print(f"Update available: v{result['current']} -> v{result['latest']}")
        print(f"  {result['url']}")
    else:
        print(f"Up to date (v{result['current']}).")


def action_backup():
    renewal_cli.cmd_backup(argparse.Namespace())


def action_restore():
    for node in nodes_mod.load_nodes(os.path.join(renewal_cli.CONFIG_DIR, "nodes.json")):
        backups = __import__("backup").list_backups(renewal_cli.BACKUP_DIR)
        print(f"Available backups: {backups}")
    name = input("Backup filename to restore (exact, or blank to cancel): ").strip()
    if not name:
        print("Cancelled.")
        return
    node_id = input("Node id to restore into: ").strip()
    if not _confirm(f"Restore {name} into node '{node_id}'? This overwrites the live ledger"):
        print("Cancelled.")
        return
    renewal_cli.cmd_rollback(argparse.Namespace(node=node_id, backup_file=name, yes=True))


def action_uninstall():
    print("This removes the generated systemd unit files (local review copies) and, with")
    print("--purge, all local config/credentials/ledger data too.")
    typed = input("Type EXACTLY 'UNINSTALL' to proceed, anything else cancels: ")
    if typed != "UNINSTALL":
        print("Cancelled.")
        return
    purge = _confirm("Also permanently delete config/credentials/ledger data (--purge)?")
    renewal_cli.cmd_uninstall(argparse.Namespace(purge=purge))


def collect_virtualizor_info() -> dict:
    v_info = {
        "installed": vinfo.detect_virtualizor_installed(),
        "version": vinfo.detect_virtualizor_version(),
        "virtualization_technologies": vinfo.detect_virtualization_technologies(),
        "api": None,
    }
    try:
        import credentials as creds_mod
        path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "self")
        if os.path.exists(path):
            creds = creds_mod.load_credentials(path)
            v_info["api"] = vinfo.probe_api_latency(creds)
    except Exception:
        pass
    return v_info


def action_system_info(style: Style, show_virtual_interfaces: bool = False):
    """v1.7.0: the obsolete 'v' sub-prompt is gone -- host info and Virtualizor info are
    ALWAYS both printed on this one screen, unconditionally, with no extra keystroke able to
    gate either section. The full (unfiltered) network-interface listing is now a completely
    separate, explicitly-labeled main-menu action (see action_system_info_advanced below),
    not a nested prompt inside this screen -- so the default and advanced views are reached
    through two visibly different menu entries, never confusable with each other."""
    info = hostinfo.collect_host_info()
    try:
        import control_center_api as api
        info["host_time_snapshot"] = api.get_host_time_overview(renewal_cli.CONFIG_DIR,
                                                                  renewal_cli.STATE_DIR)
    except Exception:
        pass  # a host-time snapshot failure must never take down the whole system-info screen
    print(render_host_info(info, style, show_virtual_interfaces=show_virtual_interfaces))
    print()
    print(render_virtualizor_info(collect_virtualizor_info(), style))


def action_system_info_advanced(style: Style):
    print(f"{style.BOLD}{style.ACCENT}=== Advanced: all network interfaces (including "
          f"virtual) ==={style.RESET}\n")
    action_system_info(style, show_virtual_interfaces=True)


def _inventory_db_path() -> str:
    return os.path.join(renewal_cli.STATE_DIR, "inventory.sqlite3")


def action_inventory_sync(node_id: str = "self", style: Style = None):
    style = style or Style(enabled=False)
    import credentials as creds_mod
    path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, node_id)
    if not os.path.exists(path):
        print(f"No stored credentials for node '{node_id}' -- configure them first.")
        return
    creds = creds_mod.load_credentials(path)
    conn = inv.open_db(_inventory_db_path())
    result = isync.run_inventory_sync(conn, node_id, creds, renewal_cli.STATE_DIR)
    if result["status"] == "OK":
        d = result["diff"]
        fetched, total = result.get("fetched_count"), result.get("authoritative_total")
        if total is not None and fetched != total:
            print(f"{style.BOLD}{style.RED}*** INCOMPLETE INVENTORY ***{style.RESET}")
            print(f"  Fetched {fetched} of {total} VPSs reported by server metadata.")
        else:
            coverage = f" (coverage {fetched}/{total})" if total is not None else ""
            print(f"{style.GREEN}Inventory sync OK{style.RESET}{coverage}: "
                  f"+{d['added_count']} added, -{d['removed_count']} removed, "
                  f"{d['modified_count']} modified (configuration changes only).")
        if d["quota_changed"]:
            print(f"  Quota changes detected on vpsid: {d['quota_changed']}")
        if d.get("usage_trend"):
            print(f"  {style.GRAY}{len(d['usage_trend'])} VPS had routine bandwidth usage "
                  f"change only (not shown as modified){style.RESET}")
    elif result["status"] == "ALREADY_RUNNING":
        print("An inventory sync is already running -- try again shortly.")
    elif result["status"] == "PARTIAL":
        print(f"{style.BOLD}{style.RED}*** INCOMPLETE INVENTORY ***{style.RESET}")
        print(f"  {result['error']}")
        fetched, total = result.get("fetched_count"), result.get("authoritative_total")
        if fetched is not None:
            print(f"  Fetched {fetched} of {total if total is not None else '?'} VPSs.")
        print("  Not treated as a change -- the previous snapshot remains current, and no "
              "VPS was reported as deleted because of this incomplete sync.")
    else:
        print(f"{style.RED}Inventory sync ERROR: {result['error']}{style.RESET}")


def _render_coverage_banner(sync_info: dict, style: Style) -> str:
    if sync_info is None:
        return f"{style.GRAY}No inventory sync has run yet.{style.RESET}"
    total = sync_info.get("authoritative_total")
    count = sync_info["record_count"]
    if total is not None and count != total:
        return (f"{style.BOLD}{style.RED}*** INCOMPLETE INVENTORY: {count} of {total} "
                f"VPSs ***{style.RESET}")
    coverage = f" (coverage {count}/{total})" if total is not None else " (coverage unknown)"
    return f"{style.GREEN}Snapshot complete{style.RESET}{coverage}"


def action_inventory_view(style: Style, node_id: str = "self"):
    conn = inv.open_db(_inventory_db_path())
    records = inv.latest_records(conn, node_id)
    settings = load_dashboard_settings(renewal_cli.CONFIG_DIR)
    threshold = Decimal(settings["usage_threshold_percent"])
    sync_info = inv.last_sync_info(conn, node_id)
    state = isync.load_sync_state(renewal_cli.STATE_DIR)
    freshness = isync.classify_freshness(
        sync_info["captured_at"] if sync_info else None, state["last_attempt_status"],
        settings["refresh_interval_minutes"])

    page = 1
    page_size = 20
    search = None
    status_filter = None
    sort_by = "vpsid"
    while True:
        # v1.7.0 fix: clear and redraw the ENTIRE screen on every loop iteration -- the
        # previous version never cleared between page/search/filter actions, so each new
        # (often shorter, filtered) table render scrolled in underneath the previous one's
        # leftover rows, producing exactly the "confusing output and repeated partial table
        # rows" the operator reported. Every iteration now starts from a clean screen.
        clear_screen()
        print(f"{style.GRAY}TixoCloud Control Center{style.RESET} {style.ACCENT}>{style.RESET} "
              f"{style.BOLD}View VPS inventory & analytics{style.RESET}")
        print(f"{style.GRAY}{'-' * 60}{style.RESET}\n")
        print(_render_coverage_banner(sync_info, style))
        print(f"Freshness: {freshness}" + (f"   Last sync: {format_local_time(sync_info['captured_at'], load_display_tz(renewal_cli.CONFIG_DIR))}" if sync_info else ""))
        print()

        filtered = filter_and_sort_vps(records, search=search, status_filter=status_filter, sort_by=sort_by)
        page_records, page, total_pages = paginate(filtered, page=page, page_size=page_size)
        print(render_vps_table(page_records, style))
        print(f"\n  Page {page}/{total_pages}  ({len(filtered)} matching of {len(records)} total)  "
              f"sorted by: {sort_by}")
        print(f"  {style.GRAY}[n]ext page  [p]rev page  [s]earch  [f]ilter status  [o]rder/sort  "
              f"[d] VPS detail  [c]lear filters  [r]efresh now (manual override)  [0] back{style.RESET}")
        choice = input("> ").strip().lower()
        if choice in ("0", ""):
            break
        elif choice == "o":
            raw = input("Sort by (id/name/quota/usage), blank keeps current: ").strip().lower()
            sort_map = {"id": "vpsid", "name": "hostname", "quota": "quota_gb", "usage": "used_gb"}
            if raw in sort_map:
                sort_by = sort_map[raw]
            page = 1
        elif choice == "n":
            page = min(page + 1, total_pages)  # clamped -- never silently drifts past the real last page
        elif choice == "p":
            page = max(page - 1, 1)
        elif choice == "s":
            search = input("Search (VPS ID, hostname): ").strip() or None
            page = 1
        elif choice == "f":
            status_filter = input("Runtime status filter (running/stopped/suspended/unavailable, "
                                   "blank clears): ").strip() or None
            page = 1
        elif choice == "c":
            search, status_filter, page = None, None, 1
        elif choice == "r":
            print(f"{style.GRAY}Inventory normally refreshes automatically on its own "
                  f"schedule -- this forces one extra read-only sync right now.{style.RESET}")
            action_inventory_sync(node_id, style=style)
            records = inv.latest_records(conn, node_id)
            sync_info = inv.last_sync_info(conn, node_id)
            state = isync.load_sync_state(renewal_cli.STATE_DIR)
            freshness = isync.classify_freshness(
                sync_info["captured_at"] if sync_info else None, state["last_attempt_status"],
                settings["refresh_interval_minutes"])
            input(f"{style.GRAY}Press Enter to continue.{style.RESET}")
        elif choice == "d":
            vpsid_raw = input("VPS ID to view: ").strip()
            try:
                vpsid = int(vpsid_raw)
                detail = next((r for r in records if r["vpsid"] == vpsid), None)
            except ValueError:
                detail = None
            if detail is None:
                print(f"{style.RED}No VPS with id {vpsid_raw!r} in the current snapshot.{style.RESET}")
            else:
                print(render_vps_detail(detail, style))
        else:
            print("Unrecognized option.")

    print()
    print(render_analytics_summary(
        analytics.compute_bandwidth_analytics(records, usage_threshold_percent=threshold), style))


def action_configure_dashboard_settings():
    settings = load_dashboard_settings(renewal_cli.CONFIG_DIR)
    try:
        refresh = input(f"Dashboard refresh interval in minutes "
                         f"[{settings['refresh_interval_minutes']}]: ").strip() \
                  or settings["refresh_interval_minutes"]
        threshold = input(f"Usage alert threshold percent "
                           f"[{settings['usage_threshold_percent']}]: ").strip() \
                    or settings["usage_threshold_percent"]
        save_dashboard_settings(renewal_cli.CONFIG_DIR, refresh_interval_minutes=refresh,
                                 usage_threshold_percent=threshold)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    print("Dashboard settings updated.")


def action_configure_hostnet_interface():
    """Topology-aware selection (see net_topology.py): auto-detected bond/bridge members
    are excluded entirely (the aggregate already represents them); when exactly one
    candidate remains with high confidence, it's offered as the default but STILL requires
    explicit operator confirmation before saving -- this function never auto-selects
    without that confirmation step, matching the 'clear operator confirmation' requirement
    even in the single-candidate case."""
    import net_topology
    result = hostnet_sync.suggest_interfaces()
    current = hostnet_sync.load_settings(renewal_cli.CONFIG_DIR).get("interface")
    print(f"Currently monitored interface: {current or '(none configured)'}")
    candidates = result["candidates"]
    if result["excluded"]:
        print(f"\n{len(result['excluded'])} interface(s) excluded (virtual, uninformative, "
              f"or a bond/bridge member that would double-count):")
        for e in result["excluded"]:
            print(f"    {e['name']}: {e['exclusion_reason']}")
    if not candidates:
        print("\nNo suitable candidate interfaces detected.")
        return
    print(f"\nCandidates (most active first):")
    for i, c in enumerate(candidates, 1):
        rx = c.get("rx_bytes"); tx = c.get("tx_bytes")
        marker = " <- recommended" if result["auto_selected"] == c["name"] else ""
        print(f"  {i}. {c['name']}  [{net_topology.recommendation_reason(c)}]  "
              f"rx={rx if rx != 'unavailable' else '?'} tx={tx if tx != 'unavailable' else '?'}{marker}")
    if result["auto_selected"]:
        print(f"\nExactly one non-overlapping candidate was identified with high confidence.")
    else:
        print(f"\nMultiple plausible choices remain (see topology notes above) -- "
              f"an explicit choice is required, nothing will be guessed.")
    default_prompt = f" [{result['auto_selected']}]" if result["auto_selected"] else ""
    raw = input(f"Select a number to monitor, or type an interface name{default_prompt}, "
                f"blank to cancel: ").strip()
    if not raw and result["auto_selected"]:
        chosen = result["auto_selected"]
    elif not raw:
        print("Cancelled.")
        return
    else:
        try:
            idx = int(raw) - 1
            chosen = candidates[idx]["name"]
        except (ValueError, IndexError):
            chosen = raw
    if not _confirm(f"Monitor interface '{chosen}' for host network accounting?"):
        print("Cancelled.")
        return
    try:
        hostnet_sync.save_monitored_interface(renewal_cli.CONFIG_DIR, chosen, confirmed=True,
                                               state_dir=renewal_cli.STATE_DIR)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    print(f"Now monitoring '{chosen}'. Sampling runs on its own schedule, independent of "
          f"renewal/inventory timers; use 'Host network traffic' to view it, or run a "
          f"sample manually with the hostnet-sync CLI command.")


LIVE_REFRESH_INTERVAL_SECONDS = 1.0


def action_view_hostnet(style: Style):
    """TRUE LIVE SCREEN (v1.7.2, Parts 1+2 of the live-traffic fix). ROOT CAUSE of the old
    behavior: this function used to render the panel ONCE and then call a blocking `input()`
    (see git history) -- nothing re-rendered until Enter was pressed. Fixed here with an
    actual refresh loop that reads live kernel counters directly (read_live_interface_
    counters, never hostnet.record_sample -- the persistent 5-minute sampler is untouched)
    and polls for a single key each tick (poll_single_key) instead of blocking on input().

    Navigation: '0' or 'q' exits immediately; Ctrl+C exits gracefully (caught, no traceback).
    Terminal state is restored on every exit path via _RawTerminalGuard's __exit__, which
    runs even when the loop exits via an exception. If stdin isn't a real tty (piped,
    non-interactive, or termios unavailable), falls back to the old static single-render
    view with an explicit note, rather than hanging or crashing."""
    settings = hostnet_sync.load_settings(renewal_cli.CONFIG_DIR)
    interface = settings.get("interface")
    tz = load_display_tz(renewal_cli.CONFIG_DIR)
    if not interface:
        print(f"{style.YELLOW}No interface configured yet -- use 'Configure host network "
              f"monitoring interface' first.{style.RESET}")
        return
    conn = hostnet.open_db(os.path.join(renewal_cli.STATE_DIR, "hostnet.sqlite3"))

    if not supports_raw_input():
        cumulative = hostnet.get_cumulative(conn, interface)
        live_rate = hostnet.compute_live_throughput(conn, interface)
        recent = hostnet.get_recent_samples(conn, interface, limit=1)
        recent_sample = recent[-1] if recent else None
        import control_center_api as api
        kernel_counter = api.get_live_network_reading(interface)
        kernel_counter["source"] = f"/sys/class/net/{interface}/statistics/{{rx,tx}}_bytes"
        kernel_counter["reset_semantics"] = (
            "Resets to zero on interface reset or host reboot -- NEVER added to the "
            "persistent recorded totals above; reflects only the time since the counter's "
            "last reset.")
        if kernel_counter["ok"]:
            kernel_counter["combined_bytes"] = kernel_counter["rx_bytes"] + kernel_counter["tx_bytes"]
        print(render_hostnet_panel(interface, cumulative, live_rate, recent_sample, style,
                                    display_tz=tz, kernel_counter=kernel_counter))
        print(f"\n  {style.GRAY}(live auto-refresh unavailable in this terminal -- showing a "
              f"single static snapshot instead){style.RESET}")
        input(f"{style.GRAY}[0] Press Enter to return{style.RESET}")
        return

    prev_counters = None  # (rx, tx, monotonic_time) from the previous tick, for delta/sec
    try:
        with _RawTerminalGuard():
            while True:
                rx, tx = read_live_interface_counters(interface)
                now_mono = time.monotonic()
                counters_ok = rx is not None and tx is not None
                live_speed = compute_live_speed(prev_counters, rx, tx, now_mono) if counters_ok else None
                prev_counters = (rx, tx, now_mono) if counters_ok else None  # a gap/unavailable tick must not be bridged into a fake rate

                cumulative = hostnet.get_cumulative(conn, interface)
                recent = hostnet.get_recent_samples(conn, interface, limit=1)
                recent_sample = recent[-1] if recent else None

                clear_screen()
                print(render_live_hostnet_panel(interface, live_speed, counters_ok, cumulative,
                                                 recent_sample, style, display_tz=tz))
                print(f"\n  {style.GRAY}[0] Back   [q] Exit Live View   [Ctrl+C] Back{style.RESET}")

                key = poll_single_key(LIVE_REFRESH_INTERVAL_SECONDS)
                if key in ("0", "q"):
                    break
    except KeyboardInterrupt:
        pass


_TRAFFIC_ANALYTICS_LABELS = (
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_24_hours", "Last 24 Hours"),
    ("current_week", "Current Week (Mon start)"),
    ("last_7_days", "Last 7 Days"),
    ("current_gregorian_month", "Current Month (Gregorian)"),
    ("previous_gregorian_month", "Previous Month (Gregorian)"),
    ("current_jalali_month", "Current Month (Jalali)"),
    ("previous_jalali_month", "Previous Month (Jalali)"),
    ("all_time", "All-Time Recorded"),
)


def render_traffic_analytics_report(bundle: dict, style: Style, display_tz: str = "UTC") -> str:
    """Renders traffic_analytics.all_periods()'s bundle as a readable report (v1.8.0). Every
    period's coverage_status/coverage_note is always shown alongside its numbers -- a period
    with NO_DATA or PARTIAL_COVERAGE must never be visually indistinguishable from one with
    genuinely-observed zero traffic (see traffic_analytics.py's module docstring)."""
    lines = [f"{style.BOLD}{style.ACCENT}HOST NETWORK -- HISTORICAL TRAFFIC ANALYTICS{style.RESET}"]
    for key, label in _TRAFFIC_ANALYTICS_LABELS:
        r = bundle.get(key)
        if r is None:
            continue
        lines.append(f"\n  {style.BOLD}{label}{style.RESET}")
        if r["coverage_status"] == traffic_analytics.COVERAGE_NO_DATA:
            lines.append(f"    {style.GRAY}No data -- {r['coverage_note']}{style.RESET}")
            continue
        lines.append(f"    Download (RX):   {_format_bytes_human(r['rx_bytes'])}")
        lines.append(f"    Upload (TX):      {_format_bytes_human(r['tx_bytes'])}")
        lines.append(f"    Combined:         {_format_bytes_human(r['combined_bytes'])}")
        if r["avg_combined_bytes_per_sec"] is not None:
            lines.append(f"    Average throughput: {_format_bytes_per_sec_human(r['avg_combined_bytes_per_sec'])}")
        if r.get("peak_combined_bytes_per_sec"):
            lines.append(f"    Peak throughput:     {_format_bytes_per_sec_human(r['peak_combined_bytes_per_sec'])}")
        if r["period_start"]:
            lines.append(f"    Period:           {format_local_time(r['period_start'], display_tz)} "
                          f"-> {format_local_time(r['period_end'], display_tz)}"
                          f"{' (in progress)' if not r['complete'] else ''}")
        if r["coverage_status"] == traffic_analytics.COVERAGE_PARTIAL:
            lines.append(f"    {style.YELLOW}Partial coverage: {r['coverage_note']}{style.RESET}")
    lines.append(f"\n  {style.GRAY}Decimal (SI) byte units. Week starts Monday. Jalali months use "
                 f"the same calendar-boundary logic as this menu's date displays. These are HOST "
                 f"interface totals -- a separate accounting system from Virtualizor VPS bandwidth "
                 f"usage (see VPS inventory).{style.RESET}")
    return "\n".join(lines)


def action_view_traffic_analytics(style: Style):
    settings = hostnet_sync.load_settings(renewal_cli.CONFIG_DIR)
    interface = settings.get("interface")
    tz = load_display_tz(renewal_cli.CONFIG_DIR)
    if not interface:
        print(f"{style.YELLOW}No interface configured yet -- use 'Configure host network "
              f"monitoring interface' first.{style.RESET}")
        return
    conn = hostnet.open_db(os.path.join(renewal_cli.STATE_DIR, "hostnet.sqlite3"))
    bundle = traffic_analytics.all_periods(conn, interface, tz_name=tz)
    print(render_traffic_analytics_report(bundle, style, display_tz=tz))


def _emergency_policy_db_path() -> str:
    return os.path.join(renewal_cli.STATE_DIR, "emergency_policy.sqlite3")


def action_configure_exhausted_bandwidth_policy(style: Style):
    """Configures the v1.8.0 exhausted-bandwidth-with-no-grant-due policy (see
    emergency_policy.py). SAFETY: this ONLY writes a local settings file read by the
    (separately invoked, read-only-toward-Virtualizor) 'Check exhausted VPS bandwidth'
    action below -- it never itself changes a customer quota, and the default remains
    DISABLED (identical to pre-v1.8.0 behavior) until the operator explicitly changes it
    here."""
    current = emp.load_policy_settings(renewal_cli.CONFIG_DIR)
    print(f"Current mode: {current.mode}")
    if current.mode == emp.MODE_EMERGENCY_GRANT:
        print(f"  Emergency grant amount: {current.emergency_grant_amount_gb} GB")
        print(f"  Cooldown: {current.cooldown_hours} hours")
        print(f"  Max grants/month: {current.max_grants_per_month}")
    print(f"\nModes: [{emp.MODE_DISABLED}] no automatic action (default/current pre-v1.8.0 "
          f"behavior), [{emp.MODE_EMERGENCY_GRANT}] propose a small rate-limited top-up, "
          f"[{emp.MODE_MANUAL_REVIEW}] queue for human review, never auto-grant.")
    raw_mode = input(f"New mode [{current.mode}]: ").strip() or current.mode
    kwargs = {"mode": raw_mode}
    if raw_mode == emp.MODE_EMERGENCY_GRANT:
        amount_raw = input(f"Emergency grant amount in GB "
                            f"[{current.emergency_grant_amount_gb}]: ").strip()
        cooldown_raw = input(f"Cooldown hours between grants for the same VPS "
                              f"[{current.cooldown_hours}]: ").strip()
        max_raw = input(f"Maximum grants per VPS per calendar month "
                         f"[{current.max_grants_per_month}]: ").strip()
        kwargs["emergency_grant_amount_gb"] = Decimal(amount_raw) if amount_raw else current.emergency_grant_amount_gb
        kwargs["cooldown_hours"] = int(cooldown_raw) if cooldown_raw else current.cooldown_hours
        kwargs["max_grants_per_month"] = int(max_raw) if max_raw else current.max_grants_per_month
    try:
        new_policy = emp.ExhaustedBandwidthPolicy(**kwargs)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    if not _confirm(f"Save exhausted-bandwidth policy as mode={new_policy.mode}?"):
        print("Cancelled.")
        return
    emp.save_policy_settings(renewal_cli.CONFIG_DIR, new_policy)
    print(f"Saved. Mode is now: {new_policy.mode}. This does NOT retroactively apply to any "
          f"already-queued review or already-proposed grant, and does NOT itself change any "
          f"customer quota -- see 'Check exhausted VPS bandwidth' to evaluate the current "
          f"fleet against this policy.")


def action_check_exhausted_bandwidth(style: Style):
    """Evaluates every VPS in the EXISTING, already-synced local inventory cache (inv.
    latest_records -- populated by the regular, independent inventory-sync timer; this
    action makes NO Virtualizor API call of its own) against the configured exhausted-
    bandwidth policy. Writes only to emergency_policy.sqlite3 (a local decision-tracking
    database, separate from both the renewal ledger and the inventory cache) -- never a
    customer quota."""
    policy = emp.load_policy_settings(renewal_cli.CONFIG_DIR)
    if policy.mode == emp.MODE_DISABLED:
        print(f"{style.GRAY}Exhausted-bandwidth policy is disabled -- nothing to check. "
              f"Use 'Configure exhausted-bandwidth policy' to enable it.{style.RESET}")
        return
    conn_inv = inv.open_db(os.path.join(renewal_cli.STATE_DIR, "inventory.sqlite3"))
    records = inv.latest_records(conn_inv, "self")
    if not records:
        print(f"{style.YELLOW}No inventory records available yet -- run an inventory sync "
              f"first.{style.RESET}")
        return
    conn_ep = emp.open_db(_emergency_policy_db_path())
    result = emp.check_fleet_exhaustion(conn_ep, records, policy)
    for item in result["actionable"]:
        print(f"  VPS {item['vpsid']} ({item['hostname']}): {item['result']}")
    print(f"\nChecked {result['total_checked']} VPS record(s) against mode={policy.mode}.")
    for label, count in sorted(result["counts"].items()):
        print(f"  {label}: {count}")


def action_view_manual_review_queue(style: Style):
    conn = emp.open_db(_emergency_policy_db_path())
    pending = emp.list_pending_reviews(conn)
    tz = load_display_tz(renewal_cli.CONFIG_DIR)
    if not pending:
        print(f"{style.GRAY}No pending manual-review items.{style.RESET}")
        return
    for item in pending:
        print(f"  [{item['id']}] VPS {item['vpsid']} -- {item['status']} -- "
              f"detected {format_local_time(item['detected_at'], tz)}")
        print(f"      Reason: {item['reason']}")
    choice = input("\nEnter an id to acknowledge/resolve, or blank to go back: ").strip()
    if not choice:
        return
    try:
        review_id = int(choice)
    except ValueError:
        print("Invalid id.")
        return
    action = input("[a]cknowledge or [r]esolve? ").strip().lower()
    if action == "a":
        who = input("Your admin identifier: ").strip() or "unknown"
        emp.acknowledge_review(conn, review_id, acknowledged_by=who)
        print("Acknowledged.")
    elif action == "r":
        notes = input("Resolution notes: ").strip()
        emp.resolve_review(conn, review_id, resolution_notes=notes)
        print("Resolved.")
    else:
        print("Cancelled.")


def action_view_emergency_grants(style: Style):
    """Views PROPOSED emergency grants and, as an explicit operator-initiated action,
    offers to apply ONE of them via emergency_policy.apply_proposed_grant -- which reuses
    writer.py's existing WRITE_APPROVED-gated, idempotent apply path UNCHANGED, plus a
    SEPARATE EMERGENCY_GRANTS_APPROVED marker this feature alone requires. Mirrors
    renewal_cli.cmd_apply's own interactive confirmation shape (type-APPLY + live-typed
    credentials) -- never a shortcut around either gate."""
    conn_ep = emp.open_db(_emergency_policy_db_path())
    proposed = emp.list_proposed_grants(conn_ep)
    tz = load_display_tz(renewal_cli.CONFIG_DIR)
    if not proposed:
        print(f"{style.GRAY}No proposed emergency grants.{style.RESET}")
        return
    for item in proposed:
        print(f"  [{item['id']}] VPS {item['vpsid']} -- {item['amount_gb']} GB -- "
              f"detected {format_local_time(item['detected_at'], tz)}")

    if not emp.is_emergency_grants_approved(renewal_cli.CONFIG_DIR):
        print(f"\n{style.YELLOW}Applying emergency grants is blocked: "
              f"{emp.emergency_grants_approval_path(renewal_cli.CONFIG_DIR)} does not exist. "
              f"WRITE_APPROVED alone is never sufficient for this feature.{style.RESET}")
        return

    choice = input("\nEnter a grant id to apply, or blank to go back: ").strip()
    if not choice:
        return
    try:
        grant_id = int(choice)
    except ValueError:
        print("Invalid id.")
        return
    row = next((g for g in proposed if g["id"] == grant_id), None)
    if row is None:
        print("That id is not in the PROPOSED list above.")
        return

    confirm_text = input(f"About to WRITE +{row['amount_gb']} GB to VPS {row['vpsid']}'s live "
                          f"quota on production Virtualizor. Type EXACTLY 'APPLY' to proceed, "
                          f"anything else cancels: ")
    if confirm_text != "APPLY":
        print("Cancelled.")
        return

    import getpass
    import local_source as ls
    import writer as writer_mod
    node_id = input("Node id [self]: ").strip() or "self"
    node = next((n for n in nodes_mod.load_nodes(renewal_cli._node_config_path())
                 if n.node_id == node_id), None)
    if node is None:
        print(f"Unknown node: {node_id}")
        return
    base_url = input("Virtualizor admin API base URL (e.g. https://host:4085/index.php): ").strip()
    api_key = getpass.getpass("Admin API key: ")
    api_pass = getpass.getpass("Admin API password: ")
    creds = writer_mod.ApiCredentials(base_url=base_url, api_key=api_key, api_pass=api_pass)

    conn_ledger = sm.open_db(node.db_path(renewal_cli.STATE_DIR))
    result = emp.apply_proposed_grant(
        conn_ep, conn_ledger, grant_id, node_id, row["vpsid"], creds,
        renewal_cli.CONFIG_DIR, renewal_cli.WRITE_APPROVAL_MARKER,
        read_live_quota_fn=lambda v: ls.fetch_single_vps_quota(v),
    )
    conn_ledger.close()
    print(f"Result: {result}")
    if result.get("status") != "APPLIED":
        print(f"{style.YELLOW}Not confirmed as applied -- the grant remains PROPOSED. See "
              f"`resolve-operation` if a ledger operation was created but is stuck.{style.RESET}")


def action_configure_telegram_bot(style: Style):
    """Configures the optional Telegram bot (v1.8.0 Part 3) -- DISABLED and DIRECT mode by
    default, exactly matching the required defaults. This action only writes local settings/
    secrets files; it never starts a bot process (the long-polling loop is not yet
    implemented, see docs/DEVELOPMENT_PROGRESS.md) and never sends a real Telegram message
    unless the operator explicitly runs the connectivity test below."""
    import telegram_bot as tbot
    settings = tgc.load_telegram_settings(renewal_cli.CONFIG_DIR)
    print(f"Enabled: {settings.enabled}")
    print(f"Connectivity mode: {settings.connectivity_mode}")
    print(f"Admins: {settings.admin_ids}  Viewers: {settings.viewer_ids}")

    raw_mode = input(f"Connectivity mode [{settings.connectivity_mode}] "
                      f"({'/'.join(tgc.VALID_CONNECTIVITY_MODES)}): ").strip() or settings.connectivity_mode
    admin_raw = input(f"Admin numeric Telegram IDs, comma-separated "
                       f"[{','.join(str(i) for i in settings.admin_ids)}]: ").strip()
    viewer_raw = input(f"Viewer numeric Telegram IDs, comma-separated "
                        f"[{','.join(str(i) for i in settings.viewer_ids)}]: ").strip()
    try:
        admin_ids = [int(x) for x in admin_raw.split(",") if x.strip()] if admin_raw else settings.admin_ids
        viewer_ids = [int(x) for x in viewer_raw.split(",") if x.strip()] if viewer_raw else settings.viewer_ids
    except ValueError:
        print("Rejected: admin/viewer IDs must be numeric Telegram user IDs, never usernames.")
        return

    kwargs = dict(connectivity_mode=raw_mode, admin_ids=admin_ids, viewer_ids=viewer_ids,
                  enabled=settings.enabled, socks5_host=settings.socks5_host,
                  socks5_port=settings.socks5_port, socks5_username=settings.socks5_username,
                  relay_host=settings.relay_host, relay_port=settings.relay_port,
                  relay_use_tls=settings.relay_use_tls, report_hour=settings.report_hour,
                  report_minute=settings.report_minute, report_timezone=settings.report_timezone)
    if raw_mode == tgc.MODE_SOCKS5:
        kwargs["socks5_host"] = input(f"SOCKS5 proxy host [{settings.socks5_host or ''}]: ").strip() \
            or settings.socks5_host
        port_raw = input(f"SOCKS5 proxy port [{settings.socks5_port or ''}]: ").strip()
        kwargs["socks5_port"] = int(port_raw) if port_raw else settings.socks5_port
        kwargs["socks5_username"] = input(f"SOCKS5 username (blank for none) "
                                           f"[{settings.socks5_username or ''}]: ").strip() \
            or settings.socks5_username
    elif raw_mode == tgc.MODE_RELAY:
        kwargs["relay_host"] = input(f"Relay worker host [{settings.relay_host or ''}]: ").strip() \
            or settings.relay_host
        rport_raw = input(f"Relay worker port [{settings.relay_port or ''}]: ").strip()
        kwargs["relay_port"] = int(rport_raw) if rport_raw else settings.relay_port
        tls_raw = input(f"Use TLS (https) to reach the relay worker? [Y/n] "
                         f"(currently {settings.relay_use_tls}): ").strip().lower()
        kwargs["relay_use_tls"] = (tls_raw != "n") if tls_raw else settings.relay_use_tls
        print(f"{style.GRAY}The relay worker must be separately enrolled/enabled on its own "
              f"host via 'Configure Relay Worker' there -- this host never runs it.{style.RESET}")

    try:
        new_settings = tgc.TelegramSettings(**kwargs)
    except ValueError as e:
        print(f"Rejected: {e}")
        return

    enable_raw = input(f"Enable the bot module now? [y/N] (enabled={settings.enabled}): ").strip().lower()
    new_settings.enabled = enable_raw == "y"

    reports_raw = input(f"Enable scheduled daily reports? [y/N] "
                         f"(currently {settings.reports_enabled}): ").strip().lower()
    reports_enabled = reports_raw == "y"
    report_hour, report_minute, report_timezone = (settings.report_hour, settings.report_minute,
                                                     settings.report_timezone)
    if reports_enabled:
        hour_raw = input(f"Report hour (0-23) [{report_hour}]: ").strip()
        minute_raw = input(f"Report minute (0-59) [{report_minute}]: ").strip()
        tz_raw = input(f"Report timezone [{report_timezone}]: ").strip()
        try:
            report_hour = int(hour_raw) if hour_raw else report_hour
            report_minute = int(minute_raw) if minute_raw else report_minute
            report_timezone = tz_raw or report_timezone
        except ValueError:
            print("Rejected: report hour/minute must be numeric.")
            return
        print(f"{style.GRAY}Reports are checked once per Telegram polling-loop iteration -- "
              f"no new systemd timer is created for this.{style.RESET}")
    try:
        new_settings = tgc.TelegramSettings(
            enabled=new_settings.enabled, connectivity_mode=new_settings.connectivity_mode,
            admin_ids=new_settings.admin_ids, viewer_ids=new_settings.viewer_ids,
            socks5_host=new_settings.socks5_host, socks5_port=new_settings.socks5_port,
            socks5_username=new_settings.socks5_username, relay_host=new_settings.relay_host,
            relay_port=new_settings.relay_port, relay_use_tls=new_settings.relay_use_tls,
            report_hour=report_hour, report_minute=report_minute,
            report_timezone=report_timezone, reports_enabled=reports_enabled)
    except ValueError as e:
        print(f"Rejected: {e}")
        return

    import getpass
    secrets_path = tgc.default_secrets_path(renewal_cli.CONFIG_DIR)
    set_token = input("Set/update the bot token now? [y/N]: ").strip().lower() == "y"
    if set_token:
        token = getpass.getpass("Bot token (never echoed, never stored in plain settings): ")
        socks5_password = None
        if new_settings.connectivity_mode == tgc.MODE_SOCKS5 and new_settings.socks5_username:
            socks5_password = getpass.getpass("SOCKS5 proxy password: ")
        relay_shared_secret = None
        if new_settings.connectivity_mode == tgc.MODE_RELAY:
            relay_shared_secret = getpass.getpass(
                "Relay shared secret (must match what was enrolled on the relay worker host): ")
        tgc.store_telegram_secrets(secrets_path, bot_token=token, socks5_password=socks5_password,
                                    relay_shared_secret=relay_shared_secret)
        print(f"Secrets stored at {secrets_path} (0600 permissions).")

    if not _confirm("Save these Telegram bot settings?"):
        print("Cancelled.")
        return
    tgc.save_telegram_settings(renewal_cli.CONFIG_DIR, new_settings)
    print("Saved.")

    if _confirm("Run a connectivity test now? (sends a real getMe call to Telegram)"):
        try:
            secrets = tgc.load_telegram_secrets(secrets_path)
        except (FileNotFoundError, tgc.TelegramConfigSecurityError) as e:
            print(f"Cannot test: {e}")
            return
        try:
            transport = tbot.build_transport(new_settings, secrets)
        except ValueError as e:
            print(f"Cannot build transport: {e}")
            return
        result = tbot.test_transport_connectivity(transport)
        print(f"Connectivity test: {'OK' if result['ok'] else 'FAILED'} -- {result['detail']}")


def action_configure_web(style: Style):
    """Configures the optional Web Control Center -- DISABLED and localhost-only by
    default, exactly matching the required defaults. Never starts a server process from
    here (see renewal_cli.py's `web-run`); only writes settings/creates the first admin
    user, same safety shape as the Telegram bot's configuration screen."""
    import web_auth as wa
    settings = wcfg.load_web_settings(renewal_cli.CONFIG_DIR)
    print(f"Enabled: {settings.enabled}")
    print(f"Bind: {settings.bind_host}:{settings.bind_port}")

    host_raw = input(f"Bind host [{settings.bind_host}] (127.0.0.1 = local only, "
                      f"0.0.0.0 = all interfaces -- never do this without a reverse proxy/TLS "
                      f"in front): ").strip() or settings.bind_host
    port_raw = input(f"Bind port [{settings.bind_port}]: ").strip()
    lang_raw = input(f"Default language [{settings.default_language}] "
                      f"({'/'.join(wcfg.VALID_LANGUAGES)}): ").strip() or settings.default_language
    theme_raw = input(f"Default theme [{settings.default_theme}] "
                       f"({'/'.join(wcfg.VALID_THEMES)}): ").strip() or settings.default_theme
    try:
        new_settings = wcfg.WebSettings(
            enabled=settings.enabled, bind_host=host_raw,
            bind_port=int(port_raw) if port_raw else settings.bind_port,
            domain=settings.domain, tls_cert_path=settings.tls_cert_path,
            tls_key_path=settings.tls_key_path, default_language=lang_raw,
            default_theme=theme_raw)
    except ValueError as e:
        print(f"Rejected: {e}")
        return

    enable_raw = input(f"Enable the Web Control Center now? [y/N] "
                        f"(enabled={settings.enabled}): ").strip().lower()
    new_settings.enabled = enable_raw == "y"

    existing_users = wa.list_users(renewal_cli.CONFIG_DIR)
    if not existing_users:
        print("\nNo Web admin user exists yet.")
        if _confirm("Create the first admin user now?"):
            username = input("Admin username: ").strip()
            import getpass
            password = getpass.getpass("Admin password (never echoed): ")
            try:
                wa.create_user(renewal_cli.CONFIG_DIR, username, password, role=wa.ROLE_ADMIN)
                print(f"Admin user {username!r} created.")
            except ValueError as e:
                print(f"Rejected: {e}")
                return

    if not _confirm("Save these Web Control Center settings?"):
        print("Cancelled.")
        return
    wcfg.save_web_settings(renewal_cli.CONFIG_DIR, new_settings)
    print(f"Saved. Start it with: renewal_cli.py web-run (only works while enabled).")


def action_configure_relay_worker(style: Style):
    """Configures THIS host as a relay worker -- i.e. this screen is run on the separate VPS
    that CAN reach Telegram, never on the Iran-side host (which instead configures RELAY
    mode + relay_host/relay_port on its own 'Configure Telegram bot' screen). DISABLED by
    default. Never starts the worker process itself (see renewal_cli.py's
    `relay-worker-run`); only writes settings/secrets."""
    settings = tgc.load_relay_worker_settings(renewal_cli.CONFIG_DIR)
    print(f"Enabled: {settings.enabled}")
    print(f"Bind: {settings.bind_host}:{settings.bind_port}")

    host_raw = input(f"Bind host [{settings.bind_host}] (0.0.0.0 to accept the Iran-side "
                      f"host's connections -- put a firewall/TLS terminator in front): ").strip() \
        or settings.bind_host
    port_raw = input(f"Bind port [{settings.bind_port}]: ").strip()
    try:
        new_settings = tgc.RelayWorkerSettings(
            enabled=settings.enabled, bind_host=host_raw,
            bind_port=int(port_raw) if port_raw else settings.bind_port,
            max_envelope_age_seconds=settings.max_envelope_age_seconds)
    except ValueError as e:
        print(f"Rejected: {e}")
        return

    enable_raw = input(f"Enable the relay worker now? [y/N] "
                        f"(enabled={settings.enabled}): ").strip().lower()
    new_settings.enabled = enable_raw == "y"

    import getpass
    secrets_path = tgc.default_relay_worker_secrets_path(renewal_cli.CONFIG_DIR)
    set_secrets = input("Set/update the bot token + shared secret now? [y/N]: ").strip().lower() == "y"
    if set_secrets:
        token = getpass.getpass("Bot token (same token the Iran-side bot uses, never echoed): ")
        shared_secret = getpass.getpass(
            "Shared secret (must match what is stored on the Iran-side host, never echoed): ")
        tgc.store_relay_worker_secrets(secrets_path, bot_token=token, shared_secret=shared_secret)
        print(f"Secrets stored at {secrets_path} (0600 permissions).")

    if not _confirm("Save these relay worker settings?"):
        print("Cancelled.")
        return
    tgc.save_relay_worker_settings(renewal_cli.CONFIG_DIR, new_settings)
    print("Saved. Start it with: renewal_cli.py relay-worker-run (only works while enabled).")


MENU_ITEMS = [
    ("Install / Initial Setup", action_install),
    ("Dashboard", None),  # needs style, wired specially below
    ("System & Virtualizor information", None),  # needs style, wired specially below
    ("Show all network interfaces (advanced)", None),  # needs style, wired specially below
    # v1.6.0: manual "Sync VPS inventory" removed from the main menu -- inventory now
    # refreshes automatically on its own systemd timer (virt-renewal-inventory-sync.timer,
    # configurable interval, entirely independent of the renewal engine's own timers). A
    # manual override ('r' refresh now) remains available from inside the inventory screen
    # itself for an operator who wants to force an immediate read-only check.
    ("View VPS inventory & analytics", None),  # needs style, wired specially below
    ("Configure/rotate Virtualizor API credentials", lambda: renewal_cli.cmd_store_credentials(
        argparse.Namespace(node=input("Node id: ").strip() or "self"))),
    ("Configure monthly gift (GB, 0 is valid)", action_configure_gift),
    ("Configure monitor/apply/notifier intervals", action_configure_intervals),
    ("Configure dashboard refresh interval & usage threshold", action_configure_dashboard_settings),
    ("Configure display timezone (default Asia/Tehran)", action_configure_timezone),
    ("Configure host network monitoring interface", action_configure_hostnet_interface),
    ("Host network traffic (live)", None),  # needs style, wired specially below
    ("Host network traffic analytics (historical)", None),  # needs style, wired specially below
    ("Configure exhausted-bandwidth policy", None),  # needs style, wired specially below
    ("Check exhausted VPS bandwidth", None),  # needs style, wired specially below
    ("Manual review queue", None),  # needs style, wired specially below
    ("Emergency grants (proposed)", None),  # needs style, wired specially below
    ("Configure Telegram bot (optional)", None),  # needs style, wired specially below
    ("Configure Web Control Center (optional)", None),  # needs style, wired specially below
    ("Configure Relay Worker (optional, run on a SEPARATE host)", None),  # wired specially below
    ("Enable automatic renewal", lambda: action_toggle_auto_renewal(True)),
    ("Disable automatic renewal", lambda: action_toggle_auto_renewal(False)),
    ("View logs and renewal history", action_view_logs),
    ("Check for updates", action_check_updates),
    ("Backup", action_backup),
    ("Restore", action_restore),
    ("Uninstall", action_uninstall),
]


def render_main_menu(style: "Style") -> None:
    print(render_banner(style))
    print(f"{style.BOLD}{style.ACCENT}TixoCloud Control Center{style.GRAY} -- v{renewal_cli.VERSION}{style.RESET}")
    print(f"{style.GRAY}{'-' * 60}{style.RESET}")
    for i, (label, _) in enumerate(MENU_ITEMS, 1):
        print(f"  {style.ACCENT}{i:>2}{style.RESET}. {label}")
    print(f"{style.GRAY}{'-' * 60}{style.RESET}")
    print(f"  {style.ACCENT} 0{style.RESET}. Exit   {style.GRAY}(q also exits, h for help){style.RESET}")


def run_menu():
    """Real screen navigation (v1.5.0): the main menu is shown exactly once per visit, not
    reprinted after every action. Selecting a section clears the screen, shows ONLY that
    section under a breadcrumb title, and returns to a freshly redrawn main menu afterward --
    never stacking one screen's output under the previous one. Terminal clearing is only
    ever attempted when is_interactive() (checked once at entry and implicitly by every
    clear_screen() call) -- a noninteractive/piped session never receives a single control
    code. Ctrl+C at any point exits cleanly instead of a raw traceback."""
    style = Style(supports_color())
    if not is_interactive():
        print("Not an interactive terminal -- use `tixo-renewal --status` for automation, "
              "or run this in a real terminal for the management menu.")
        return 1
    try:
        while True:
            clear_screen()
            render_main_menu(style)
            choice = input("Choose an option: ").strip()
            if choice in ("0", "q", "Q"):
                clear_screen()
                return 0
            if choice.lower() == "h":
                input(f"\n{style.GRAY}Navigate by number. 0 or q exits. Dangerous actions "
                      f"(uninstall, restore) require an explicit typed confirmation. No "
                      f"credential is ever printed. Press Enter to continue.{style.RESET}")
                continue
            try:
                idx = int(choice) - 1
                label, fn = MENU_ITEMS[idx]
            except (ValueError, IndexError):
                input(f"{style.RED}Invalid choice. Press Enter to continue.{style.RESET}")
                continue

            clear_screen()
            print(f"{style.GRAY}TixoCloud Control Center{style.RESET} {style.ACCENT}>{style.RESET} "
                  f"{style.BOLD}{label}{style.RESET}")
            print(f"{style.GRAY}{'-' * 60}{style.RESET}\n")
            try:
                if label == "Dashboard":
                    action_dashboard(style)
                elif label == "System & Virtualizor information":
                    action_system_info(style)
                elif label == "Show all network interfaces (advanced)":
                    action_system_info_advanced(style)
                elif label == "View VPS inventory & analytics":
                    action_inventory_view(style)
                elif label == "Host network traffic (live)":
                    action_view_hostnet(style)
                elif label == "Host network traffic analytics (historical)":
                    action_view_traffic_analytics(style)
                elif label == "Configure exhausted-bandwidth policy":
                    action_configure_exhausted_bandwidth_policy(style)
                elif label == "Check exhausted VPS bandwidth":
                    action_check_exhausted_bandwidth(style)
                elif label == "Manual review queue":
                    action_view_manual_review_queue(style)
                elif label == "Emergency grants (proposed)":
                    action_view_emergency_grants(style)
                elif label == "Configure Telegram bot (optional)":
                    action_configure_telegram_bot(style)
                elif label == "Configure Web Control Center (optional)":
                    action_configure_web(style)
                elif label == "Configure Relay Worker (optional, run on a SEPARATE host)":
                    action_configure_relay_worker(style)
                else:
                    fn()
            except Exception as e:
                print(f"{style.RED}Error: {e}{style.RESET}")
            input(f"\n{style.GRAY}[0] Press Enter to return to the main menu{style.RESET}")
    except KeyboardInterrupt:
        clear_screen()
        print("\nExiting.")
        return 0


def main():
    if "--status" in sys.argv:
        return renewal_cli.cmd_status(argparse.Namespace())
    if "--version" in sys.argv:
        print(renewal_cli.VERSION)
        return 0
    force_cli = "--cli" in sys.argv
    try:
        import tui_textual
    except Exception:
        tui_textual = None
    if tui_textual is not None and tui_textual.should_use_textual(
            force_cli=force_cli, is_interactive=is_interactive()):
        try:
            return tui_textual.run_textual_app(renewal_cli, sys.modules[__name__])
        except Exception as e:
            print(f"Textual UI failed to start ({e}) -- falling back to the lightweight "
                  f"CLI menu.")
    return run_menu()


if __name__ == "__main__":
    sys.exit(main())
