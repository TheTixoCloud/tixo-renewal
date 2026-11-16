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
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import nodes as nodes_mod
import policy as pol
import renewal_cli
import shadow_monitor as sm

GITHUB_API_LATEST_RELEASE = "https://api.github.com/repos/TheTixoCloud/tixo-renewal/releases/latest"


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
            conn.close()
            per_node[n.node_id] = {
                "initialized": True,
                "last_poll_at": checkpoint[0] if checkpoint else None,
                "operations_total": ops_total,
                "operations_unresolved": ops_unresolved,
            }
        data["per_node"] = per_node
    except Exception as e:
        data["nodes_error"] = str(e)
    data["timers"] = collect_timer_status()
    return data


def render_dashboard(data: dict, style: Style) -> str:
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
        lines.append(f"    Last poll: {info.get('last_poll_at') or 'never'}")
        lines.append(f"    Operations: {info['operations_total']} total, "
                      f"{info['operations_unresolved']} unresolved")
    lines.append(f"\n  {style.BOLD}Timers:{style.RESET}")
    for unit, status in data.get("timers", {}).items():
        active = status["active"]
        color = style.GREEN if active == "active" else style.DIM
        lines.append(f"    {unit}: enabled={status['enabled']} {color}active={active}{style.RESET}")
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
    print(render_dashboard(data, style))


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


MENU_ITEMS = [
    ("Install / Initial Setup", action_install),
    ("Dashboard", None),  # needs style, wired specially below
    ("Configure Virtualizor API credentials", lambda: renewal_cli.cmd_store_credentials(
        argparse.Namespace(node=input("Node id: ").strip() or "self"))),
    ("Configure monthly gift (GB, 0 is valid)", action_configure_gift),
    ("Configure monitor/apply/notifier intervals", action_configure_intervals),
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
        print(f"\n{style.BOLD}{style.CYAN}Tixo Renewal v{renewal_cli.VERSION}{style.RESET}")
        for i, (label, _) in enumerate(MENU_ITEMS, 1):
            print(f"  {i}. {label}")
        print(f"  0. Exit")
        choice = input("Choose an option: ").strip()
        if choice == "0":
            return 0
        try:
            idx = int(choice) - 1
            label, fn = MENU_ITEMS[idx]
        except (ValueError, IndexError):
            print("Invalid choice.")
            continue
        try:
            if label == "Dashboard":
                action_dashboard(style)
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
