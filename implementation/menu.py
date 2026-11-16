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

GITHUB_API_LATEST_RELEASE = "https://api.github.com/repos/TheTixoCloud/tixo-renewal/releases/latest"
DEFAULT_DISPLAY_TZ = "Asia/Tehran"

BANNER = r"""
  _____ _           _____ _                 _
 |_   _(_)_  _____ / ____| |               | |
   | |  \ \/ / _ \ |      | | ___  _   _  __| |
   | |   >  < (_) | |     | |/ _ \| | | |/ _` |
   |_|  /_/\_\___/| |____ | | (_) | |_| | (_| |
                    \_____|_|\___/ \__,_|\__,_|
         Virtualizor Bandwidth Renewal
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
    render (never let a display bug crash the menu)."""
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_utc_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(tz_name))
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


class Style:
    """ANSI codes, all-empty when color is unsupported -- every caller just uses these
    constants unconditionally, so there is exactly one place that decides color on/off."""
    def __init__(self, enabled: bool):
        self.RESET = "\033[0m" if enabled else ""
        self.BOLD = "\033[1m" if enabled else ""
        self.GREEN = "\033[32m" if enabled else ""
        self.RED = "\033[31m" if enabled else ""
        self.YELLOW = "\033[33m" if enabled else ""
        self.CYAN = "\033[36m" if enabled else ""
        self.DIM = "\033[2m" if enabled else ""


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
                 "virt-renewal-readiness-notifier.timer"):
        enabled = _systemctl("is-enabled", unit)
        active = _systemctl("is-active", unit)
        out[unit] = {"enabled": enabled, "active": active}
    return out


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
    return data


def render_dashboard(data: dict, style: Style, display_tz: str = "UTC") -> str:
    lines = [f"{style.BOLD}{style.CYAN}=== Tixo Renewal Dashboard ==={style.RESET}"]
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
    lines.append(f"\n  {style.BOLD}Timers:{style.RESET}")
    for unit, status in data.get("timers", {}).items():
        active = status["active"]
        color = style.GREEN if active == "active" else style.DIM
        lines.append(f"    {unit}: enabled={status['enabled']} {color}active={active}{style.RESET}")
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


def render_host_info(info: dict, style: Style) -> str:
    lines = [f"{style.BOLD}{style.CYAN}=== Host System ==={style.RESET}"]
    os_info = info["os"]
    lines.append(f"  Hostname: {os_info['hostname']}  OS: {os_info['distribution']}  "
                  f"Kernel: {os_info['kernel']} ({os_info['architecture']})")
    cpu = info["cpu"]
    lines.append(f"  CPU: {cpu['vendor']} / {cpu['model']}  "
                  f"cores={cpu['physical_cores']} logical={cpu['logical_cpus']} "
                  f"threads/core={cpu['threads_per_core']}")
    mem = info["memory"]
    usage = info["usage"]
    lines.append(f"  Memory: {mem['total_mb']} MB total, {mem['available_mb']} MB available "
                  f"{render_progress_bar(usage['memory_percent'], style=style)}")
    lines.append(f"  Swap: {mem['swap_total_mb']} MB total, {mem['swap_free_mb']} MB free")
    for d in info["disks"]:
        lines.append(f"  Disk {d['mount_point']}: {d['used_gb']}/{d['total_gb']} GB used "
                     f"{render_progress_bar(d['percent_used'], style=style)}")
    up = info["uptime"]
    lines.append(f"  Uptime: {up['uptime_seconds']}s  Load: {up['load_average']}")
    temp = info["temperature"]
    if temp["available"]:
        zones = ", ".join(f"{z['type']}={z['celsius']}C" for z in temp["zones"])
        lines.append(f"  Temperature: {zones}")
    else:
        lines.append(f"  Temperature: {style.DIM}unavailable ({temp['reason']}){style.RESET}")
    for iface in info["network_interfaces"]:
        lines.append(f"  Interface {iface['name']}: state={iface['state']} ipv4={iface['ipv4']}")
    lines.append(f"  Host system timezone: {info['host_timezone']}")
    return "\n".join(lines)


def render_virtualizor_info(v_info: dict, style: Style) -> str:
    lines = [f"{style.BOLD}{style.CYAN}=== Virtualizor ==={style.RESET}"]
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
    lines = [f"{style.BOLD}{style.CYAN}=== Bandwidth Analytics ==={style.RESET}"]
    lines.append(f"  Total VPS: {result['total_vps']}  Limited: {result['limited_vps_count']}  "
                 f"Unlimited: {result['unlimited_vps_count']}  Unknown quota: {result['unknown_quota_vps_count']}")
    lines.append(f"  Running: {result['running_count']}  Stopped/suspended: {result['stopped_count']}")
    lines.append(f"  Limited fleet: {result['limited_used_sum_gb']} / {result['limited_quota_sum_gb']} GB used "
                 f"(coverage {result['limited_used_sum_coverage']}), "
                 f"{result['limited_remaining_sum_gb']} GB remaining")
    lines.append(f"  Unlimited fleet actual usage: {result['unlimited_used_sum_gb']} GB "
                 f"(coverage {result['unlimited_used_sum_coverage']})")
    if result["vps_over_threshold"]:
        lines.append(f"  {style.YELLOW}Over usage threshold: {result['vps_over_threshold']}{style.RESET}")
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
               or s in str(r.get("hostname", "")).lower()]
    if status_filter:
        out = [r for r in out if str(r.get("status", "")).lower() == status_filter.lower()]
    try:
        out = sorted(out, key=lambda r: (str(r.get(sort_by, "")) if sort_by not in
                     ("vpsid",) else r.get(sort_by, 0)))
    except TypeError:
        pass
    return out


def render_vps_table(records: list[dict], style: Style) -> str:
    if not records:
        return f"  {style.DIM}(no VPS records -- run an inventory sync first){style.RESET}"
    header = f"  {'VPSID':>7}  {'HOSTNAME':<24}  {'STATUS':<10}  {'QUOTA_GB':>10}  {'USED_GB':>10}"
    lines = [style.BOLD + header + style.RESET]
    for r in records:
        lines.append(f"  {r['vpsid']:>7}  {str(r.get('hostname'))[:24]:<24}  "
                     f"{str(r.get('status'))[:10]:<10}  {str(r.get('quota_gb')):>10}  "
                     f"{str(r.get('used_gb')):>10}")
    return "\n".join(lines)


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
    current = load_display_tz(renewal_cli.CONFIG_DIR)
    raw = input(f"Display timezone (IANA name, e.g. Asia/Tehran) [{current}]: ").strip() or current
    try:
        save_display_tz(renewal_cli.CONFIG_DIR, raw)
    except ValueError as e:
        print(f"Rejected: {e}")
        return
    print(f"Display timezone set to {raw}. This only affects how times are SHOWN in this "
          f"menu -- the host's actual system timezone is never changed.")


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


def action_system_info(style: Style):
    print(render_host_info(hostinfo.collect_host_info(), style))
    v_info = {
        "installed": vinfo.detect_virtualizor_installed(),
        "version": vinfo.detect_virtualizor_version(),
        "virtualization_technologies": vinfo.detect_virtualization_technologies(),
        "api": None,
    }
    node_id = "self"
    try:
        import credentials as creds_mod
        path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, node_id)
        if os.path.exists(path):
            creds = creds_mod.load_credentials(path)
            v_info["api"] = vinfo.probe_api_latency(creds)
    except Exception:
        pass
    print(render_virtualizor_info(v_info, style))


def _inventory_db_path() -> str:
    return os.path.join(renewal_cli.STATE_DIR, "inventory.sqlite3")


def action_inventory_sync(node_id: str = "self"):
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
        print(f"Inventory sync OK: +{d['added_count']} added, -{d['removed_count']} removed, "
              f"{d['modified_count']} modified.")
        if d["quota_changed"]:
            print(f"  Quota changes detected on vpsid: {d['quota_changed']}")
    elif result["status"] == "ALREADY_RUNNING":
        print("An inventory sync is already running -- try again shortly.")
    elif result["status"] == "PARTIAL":
        print(f"Sync returned a PARTIAL/incomplete response ({result['error']}) -- "
              f"not treated as a change, previous snapshot kept as current.")
    else:
        print(f"Inventory sync ERROR: {result['error']}")


def action_inventory_view(style: Style, node_id: str = "self"):
    conn = inv.open_db(_inventory_db_path())
    records = inv.latest_records(conn, node_id)
    settings = load_dashboard_settings(renewal_cli.CONFIG_DIR)
    threshold = Decimal(settings["usage_threshold_percent"])
    print(render_vps_table(records, style))
    print()
    print(render_analytics_summary(
        analytics.compute_bandwidth_analytics(records, usage_threshold_percent=threshold), style))
    sync_info = inv.last_sync_info(conn, node_id)
    state = isync.load_sync_state(renewal_cli.STATE_DIR)
    freshness = isync.classify_freshness(
        sync_info["captured_at"] if sync_info else None, state["last_attempt_status"],
        settings["refresh_interval_minutes"])
    print(f"\n  Snapshot freshness: {freshness}")


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


MENU_ITEMS = [
    ("Install / Initial Setup", action_install),
    ("Dashboard", None),  # needs style, wired specially below
    ("System & Virtualizor information", None),  # needs style, wired specially below
    ("Sync VPS inventory (read-only)", action_inventory_sync),
    ("View VPS inventory & analytics", None),  # needs style, wired specially below
    ("Configure/rotate Virtualizor API credentials", lambda: renewal_cli.cmd_store_credentials(
        argparse.Namespace(node=input("Node id: ").strip() or "self"))),
    ("Configure monthly gift (GB, 0 is valid)", action_configure_gift),
    ("Configure monitor/apply/notifier intervals", action_configure_intervals),
    ("Configure dashboard refresh interval & usage threshold", action_configure_dashboard_settings),
    ("Configure display timezone (default Asia/Tehran)", action_configure_timezone),
    ("Enable automatic renewal", lambda: action_toggle_auto_renewal(True)),
    ("Disable automatic renewal", lambda: action_toggle_auto_renewal(False)),
    ("View logs and renewal history", action_view_logs),
    ("Check for updates", action_check_updates),
    ("Backup", action_backup),
    ("Restore", action_restore),
    ("Uninstall", action_uninstall),
]


def run_menu():
    style = Style(supports_color())
    if not is_interactive():
        print("Not an interactive terminal -- use `tixo-renewal --status` for automation, "
              "or run this in a real terminal for the management menu.")
        return 1
    while True:
        print(f"{style.CYAN}{BANNER}{style.RESET}")
        print(f"{style.BOLD}{style.CYAN}Tixo Renewal v{renewal_cli.VERSION}{style.RESET}")
        for i, (label, _) in enumerate(MENU_ITEMS, 1):
            print(f"  {i}. {label}")
        print(f"  0. Exit  (h for help)")
        choice = input("Choose an option: ").strip()
        if choice == "0":
            return 0
        if choice.lower() == "h":
            print("Navigate by number. 0 exits. Dangerous actions (uninstall, restore) "
                  "require an explicit typed confirmation. No credential is ever printed.")
            continue
        try:
            idx = int(choice) - 1
            label, fn = MENU_ITEMS[idx]
        except (ValueError, IndexError):
            print("Invalid choice.")
            continue
        try:
            if label == "Dashboard":
                action_dashboard(style)
            elif label == "System & Virtualizor information":
                action_system_info(style)
            elif label == "View VPS inventory & analytics":
                action_inventory_view(style)
            else:
                fn()
        except Exception as e:
            print(f"{style.RED}Error: {e}{style.RESET}")


def main():
    if "--status" in sys.argv:
        return renewal_cli.cmd_status(argparse.Namespace())
    if "--version" in sys.argv:
        print(renewal_cli.VERSION)
        return 0
    return run_menu()


if __name__ == "__main__":
    sys.exit(main())
