"""Shared host time/uptime/boot/monitoring service (v1.8.0 Part 8) -- one implementation for
the CLI, the shared backend (`control_center_api.py`), the Telegram bot, and scheduled
reports. Never re-derived independently by any of them.

SOURCES, all local, zero Virtualizor API calls, zero subprocess calls:
  - Uptime: /proc/uptime (a monotonic counter maintained by the kernel since boot -- NOT
    wall-clock-dependent, so it is never confused by a clock adjustment/NTP correction the
    way "now minus a stored boot timestamp" alone would be).
  - Boot time: derived as `now - uptime_seconds` (the same method `uptime`/`who -b` use) --
    an estimate, not a kernel-reported absolute value, since Linux does not expose one
    directly; documented as such rather than overclaimed.
  - Boot ID: /proc/sys/kernel/random/boot_id (already read by hostnet.get_boot_id() --
    reused here, never a second implementation of that one-line file read).
  - Monitoring activation/elapsed/last-sample/coverage: hostnet.py's existing cumulative/
    sample tables (first_sample_at, last recorded sample, discontinuity_type) -- read-only.
  - Live kernel RX/TX counters: hostinfo's existing `_read_interface_counter` primitive
    (the SAME one menu.py's live screen and control_center_api.get_live_network_reading
    already use) -- one raw read, never a rate (a rate needs two reads with elapsed time,
    which is the CALLER's job if wanted).

CALENDAR/TIMEZONE RULES:
  - The host's actual system timezone and clock are NEVER touched -- this module only reads
    (`hostinfo.get_host_timezone()` for a DEFAULT SUGGESTION) and otherwise works entirely
    off the operator-configured DISPLAY timezone already established by menu.py
    (`load_display_tz`/`save_display_tz`, unchanged, reused here).
  - Calendar preference is a SEPARATE, additive setting (`calendar_preference`, stored in
    the SAME display-settings.json file menu.py already manages, as an additive key) with
    four values: AUTO (today's established behavior -- Jalali only when the display
    timezone is exactly "Asia/Tehran", Gregorian otherwise), JALALI (always), GREGORIAN
    (always), BOTH (show both calendars together). Defaults to AUTO -- zero behavior change
    for any existing installation.
  - All stored timestamps remain UTC ISO-8601 throughout the project; this module only ever
    converts for DISPLAY, never alters what's persisted.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNAVAILABLE = "unavailable"

CALENDAR_AUTO = "auto"
CALENDAR_JALALI = "jalali"
CALENDAR_GREGORIAN = "gregorian"
CALENDAR_BOTH = "both"
VALID_CALENDAR_PREFERENCES = (CALENDAR_AUTO, CALENDAR_JALALI, CALENDAR_GREGORIAN, CALENDAR_BOTH)


# ---------- uptime / boot time -----------------------------------------------------------------

def read_uptime_seconds() -> float:
    """Reads /proc/uptime directly (the first field: seconds since boot, monotonic, never
    affected by clock adjustments). Returns UNAVAILABLE (never 0, never a guess) if the file
    can't be read or parsed."""
    try:
        text = Path("/proc/uptime").read_text()
        return float(text.split()[0])
    except (OSError, ValueError, IndexError):
        return UNAVAILABLE


def format_duration_human(seconds) -> str:
    """Days, hours, minutes, AND seconds (always all four units present, even for a very
    short uptime -- e.g. "0d 0h 0m 5s" -- so this is unambiguous at a glance, distinct from
    menu.py's existing shorter `_format_uptime_human`, which deliberately omits seconds for
    the main dashboard's compact display and is left untouched)."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return UNAVAILABLE
    if seconds < 0:
        return UNAVAILABLE
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m {secs}s"


def compute_boot_time_utc(uptime_seconds, now: datetime = None) -> datetime:
    """Returns the ESTIMATED UTC boot time (now - uptime), or None if uptime is unavailable/
    unparseable -- never fabricated. This is an estimate (the standard `uptime`/`who -b`
    method), not a kernel-reported absolute timestamp, since Linux exposes only the
    monotonic counter, not an absolute boot instant."""
    try:
        uptime_seconds = float(uptime_seconds)
    except (TypeError, ValueError):
        return None
    if uptime_seconds < 0:
        return None
    now = now or datetime.now(timezone.utc)
    return now - timedelta(seconds=uptime_seconds)


def get_current_boot_id() -> str:
    import hostnet
    return hostnet.get_boot_id()


# ---------- calendar preference (additive key in menu.py's display-settings.json) -----------

def _settings_path(config_dir: str) -> str:
    import menu as menu_mod
    return menu_mod.display_settings_path(config_dir)


def load_calendar_preference(config_dir: str) -> str:
    path = _settings_path(config_dir)
    if not os.path.exists(path):
        return CALENDAR_AUTO
    try:
        value = json.loads(Path(path).read_text()).get("calendar_preference", CALENDAR_AUTO)
        return value if value in VALID_CALENDAR_PREFERENCES else CALENDAR_AUTO
    except (OSError, ValueError, TypeError):
        return CALENDAR_AUTO


def save_calendar_preference(config_dir: str, preference: str) -> None:
    """Merges into the SAME display-settings.json menu.py already writes `display_tz` to --
    never a second, divergent settings file for what is conceptually one "how to show
    dates/times" setting. Preserves any existing display_tz value already in the file."""
    if preference not in VALID_CALENDAR_PREFERENCES:
        raise ValueError(f"invalid calendar preference: {preference!r} "
                          f"(must be one of {VALID_CALENDAR_PREFERENCES})")
    path = _settings_path(config_dir)
    existing = {}
    if os.path.exists(path):
        try:
            existing = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            existing = {}
    existing["calendar_preference"] = preference
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(existing, indent=2) + "\n")
    os.replace(tmp, path)


def detect_default_display_timezone() -> str:
    """Suggests the HOST's own system timezone (read-only, via /etc/localtime -- never
    altered) as the default for the operator-configured DISPLAY timezone, validated against
    zoneinfo. Falls back to menu.py's existing "Asia/Tehran" project default (preserving
    every existing installation's behavior exactly) if the host timezone can't be read or
    isn't a valid IANA name zoneinfo recognizes."""
    import hostinfo
    import menu as menu_mod
    host_tz = hostinfo.get_host_timezone()
    if host_tz == UNAVAILABLE or host_tz == hostinfo.UNAVAILABLE:
        return menu_mod.DEFAULT_DISPLAY_TZ
    try:
        ZoneInfo(host_tz)
    except (ZoneInfoNotFoundError, ValueError):
        return menu_mod.DEFAULT_DISPLAY_TZ
    return host_tz


# ---------- calendar-preference-aware local time formatting --------------------------------

def format_local_datetime(dt_or_iso, tz_name: str, calendar_preference: str = CALENDAR_AUTO) -> str:
    """Formats a UTC datetime (or ISO string) for display in `tz_name`, honoring
    `calendar_preference`. Always 24-hour clock time. Never raises -- falls back to the raw
    input string on any parse failure, matching menu.format_local_time's existing
    never-crash-a-render discipline."""
    import jalali

    try:
        dt = (datetime.fromisoformat(dt_or_iso) if isinstance(dt_or_iso, str) else dt_or_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return str(dt_or_iso)

    time_part = local.strftime("%H:%M:%S %Z")
    gregorian_date = local.strftime("%Y-%m-%d")
    jalali_date = jalali.format_jalali_date(local)

    pref = calendar_preference if calendar_preference in VALID_CALENDAR_PREFERENCES else CALENDAR_AUTO
    if pref == CALENDAR_AUTO:
        pref = CALENDAR_JALALI if tz_name == "Asia/Tehran" else CALENDAR_GREGORIAN

    if pref == CALENDAR_JALALI:
        return f"{jalali_date} {time_part} (Jalali)"
    if pref == CALENDAR_BOTH:
        return f"{gregorian_date} / {jalali_date} {time_part}"
    return f"{gregorian_date} {time_part}"


# ---------- full snapshot: the one function the shared service layer wraps ------------------

def get_host_time_snapshot(config_dir: str, state_dir: str, interface: str = None,
                            now: datetime = None) -> dict:
    """Everything this module offers, in one read-only, API-call-free bundle:
    current server-local date/time, uptime, boot time, boot ID, monitoring activation date/
    elapsed duration, last persistent sample, data coverage, and a clearly separate raw
    kernel-counter reading (never merged into persistent recorded totals -- callers that
    want those should use hostnet.get_cumulative()/control_center_api.get_network_overview
    directly; this function only ever surfaces the live kernel reading alongside, labeled,
    never combined)."""
    import menu as menu_mod
    import hostnet

    now = now or datetime.now(timezone.utc)
    tz_name = menu_mod.load_display_tz(config_dir)
    calendar_preference = load_calendar_preference(config_dir)

    uptime_seconds = read_uptime_seconds()
    boot_time_utc = compute_boot_time_utc(uptime_seconds, now=now)

    snapshot = {
        "generated_at_utc": now.isoformat(),
        "display_timezone": tz_name,
        "calendar_preference": calendar_preference,
        "current_local": format_local_datetime(now, tz_name, calendar_preference),
        "uptime_seconds": uptime_seconds,
        "uptime_human": format_duration_human(uptime_seconds),
        "boot_time_utc": boot_time_utc.isoformat() if boot_time_utc else None,
        "boot_time_local": (format_local_datetime(boot_time_utc, tz_name, calendar_preference)
                             if boot_time_utc else UNAVAILABLE),
        "boot_id": get_current_boot_id(),
    }

    if not interface:
        snapshot["network_configured"] = False
        return snapshot

    snapshot["network_configured"] = True
    snapshot["interface"] = interface
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    try:
        cumulative = hostnet.get_cumulative(conn, interface)
        recent = hostnet.get_recent_samples(conn, interface, limit=1)
    finally:
        conn.close()
    recent_sample = recent[-1] if recent else None

    if cumulative and cumulative.get("first_sample_at"):
        first_sample_utc = cumulative["first_sample_at"]
        snapshot["monitoring_activation_utc"] = first_sample_utc
        snapshot["monitoring_activation_local"] = format_local_datetime(
            first_sample_utc, tz_name, calendar_preference)
        try:
            elapsed_seconds = (now - datetime.fromisoformat(first_sample_utc)).total_seconds()
            snapshot["monitoring_elapsed_human"] = format_duration_human(elapsed_seconds)
        except (ValueError, TypeError):
            snapshot["monitoring_elapsed_human"] = UNAVAILABLE
    else:
        snapshot["monitoring_activation_utc"] = None
        snapshot["monitoring_activation_local"] = UNAVAILABLE
        snapshot["monitoring_elapsed_human"] = UNAVAILABLE

    if recent_sample:
        snapshot["last_persistent_sample_utc"] = recent_sample["sampled_at"]
        snapshot["last_persistent_sample_local"] = format_local_datetime(
            recent_sample["sampled_at"], tz_name, calendar_preference)
        snapshot["data_coverage_gap_seconds"] = recent_sample.get("gap_seconds")
        snapshot["data_coverage_discontinuity"] = recent_sample.get("discontinuity_type")
    else:
        snapshot["last_persistent_sample_utc"] = None
        snapshot["last_persistent_sample_local"] = UNAVAILABLE
        snapshot["data_coverage_gap_seconds"] = None
        snapshot["data_coverage_discontinuity"] = None

    import control_center_api as api
    live = api.get_live_network_reading(interface)
    snapshot["kernel_counter"] = {
        "ok": live["ok"],
        "rx_bytes": live["rx_bytes"],
        "tx_bytes": live["tx_bytes"],
        "combined_bytes": (live["rx_bytes"] + live["tx_bytes"]) if live["ok"] else None,
        "source": f"/sys/class/net/{interface}/statistics/{{rx,tx}}_bytes",
        "reset_semantics": "Resets to zero on interface reset or host reboot -- this raw "
                            "value is NEVER added to the persistent recorded totals above; "
                            "it reflects only the time since the counter's last reset.",
    }
    return snapshot
