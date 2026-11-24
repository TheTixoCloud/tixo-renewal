#!/usr/bin/env python3
"""Operator CLI for the Virtualizor Bandwidth Rollover.

Every subcommand except `apply` is read-only with respect to production, by construction:
they only ever call shadow_monitor.fetch_live_rows() (a single already-vetted SELECT script)
and read/write LOCAL SQLite files under shadow-monitor-state/. `apply` -- the only command
that would ever write a quota to Virtualizor -- is structurally blocked (see below) until the
operator deliberately creates an approval marker file. No install, status, dry-run, verify,
backup, or rollback action can ever write to production.

Commands:
    install     Idempotent local setup: directories, default node config, systemd units.
    status      Per-node summary: last poll, identities tracked, pending reviews, alerts.
    dry-run     Read-only poll + detection across all configured nodes. Never applies anything.
    verify      Run the full test suite + a live read-only reachability check per node.
    backup      Snapshot every node's SQLite ledger into backups/.
    rollback    Restore a node's ledger from a named backup (asks for confirmation).
    uninstall   Disable/remove the local systemd timer and unit files. Data is kept unless --purge.
    apply       BLOCKED by design unless renewal-config/WRITE_APPROVED exists (operator-created).
"""
from __future__ import annotations

VERSION = "1.10.10"

# Minimal spacing between consecutive real API calls across DIFFERENT VPS within one
# apply-due run -- a basic, sensible rate-limit control so a host with many eligible VPS at
# once never hammers Virtualizor's admin API in a tight loop. Deliberately small (this is not
# a substitute for Virtualizor's own rate limiting, if any) and only applied AFTER an attempt
# that actually touched the API -- never after a pure skip (no API call made at all).
API_CALL_SPACING_SECONDS = 0.5

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import alerts
import backup as backup_mod
import nodes as nodes_mod
import credentials as creds_mod
import policy as pol
import policy_integration as pi
import shadow_monitor as sm

# On the Ubuntu dev server, WORKSPACE_ROOT defaults to the existing audit workspace layout.
# On a Virtualizor host (host-local deployment via deploy/install.sh), the installer sets
# TIXO_RENEWAL_INSTALL_DIR so this exact same CLI/file serves both contexts unmodified --
# no separate "host_cli.py" to keep in sync.
WORKSPACE_ROOT = os.environ.get("TIXO_RENEWAL_INSTALL_DIR") or os.path.join(os.path.dirname(__file__), "..")
STATE_DIR = os.path.join(WORKSPACE_ROOT, "shadow-monitor-state")
CONFIG_DIR = os.path.join(WORKSPACE_ROOT, "renewal-config")
BACKUP_DIR = os.path.join(WORKSPACE_ROOT, "backups")
SYSTEMD_OUT_DIR = os.path.join(WORKSPACE_ROOT, "systemd")
WRITE_APPROVAL_MARKER = os.path.join(CONFIG_DIR, "WRITE_APPROVED")

# TIXO_RENEWAL_LOCK_PATH is shared between the monitor and apply services (NOT the notifier,
# which only reads and is lower-urgency) -- both touch the same per-node SQLite ledger
# (snapshots + operations tables). systemd itself already guarantees a single unit can never
# have two concurrent instances of ITSELF, but monitor and apply are two DIFFERENT units and
# could otherwise run at once. `flock -n` (non-blocking: skip this run rather than queue/
# block, since a missed 5-minute tick is harmless and queuing could cause drift/backlog)
# mirrors the exact pattern already used by this same host's pre-existing virt-carryover-*
# cron jobs (/run/virt-carryover-guard.lock etc.) -- not a new convention, the established one.
TIXO_RENEWAL_LOCK_PATH = "/run/tixo-renewal.lock"

SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Virtualizor Bandwidth Rollover monitor (read-only dry-run)
After=network-online.target

[Service]
Type=oneshot
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart=/usr/bin/flock -n {lockpath} {python} {cli} dry-run
WorkingDirectory={workdir}
User={user}
"""

TIMER_UNIT_TEMPLATE = """[Unit]
Description=Run the Virtualizor Bandwidth Rollover dry-run approximately every {interval}min

[Timer]
OnBootSec=2min
OnUnitActiveSec={interval}min
Unit=virt-renewal-monitor.service

[Install]
WantedBy=timers.target
"""


def validate_interval_minutes(value, min_minutes: int = 1, max_minutes: int = 180) -> int:
    """Strict validator for any operator-supplied timer interval -- these values are
    interpolated directly into a systemd unit file's OnUnitActiveSec= line, so this is the
    one place that must reject anything that isn't a plain positive integer in a sane range
    BEFORE it ever reaches a template string. Never pass an unvalidated value to a template."""
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"interval must be a whole number of minutes, got {value!r}")
    if not (min_minutes <= minutes <= max_minutes):
        raise ValueError(f"interval must be between {min_minutes} and {max_minutes} minutes, got {minutes}")
    return minutes

# UNATTENDED RENEWAL (apply) unit -- generated by `install` for review ONLY. Never enabled by
# this software, never copied anywhere by install.sh, never coupled to any approval marker
# creation. Actually applying a renewal still requires --execute AND typed 'APPLY' AND a
# non-interactive run still refuses outright unless WRITE_APPROVED exists -- this template
# exists purely so an operator who HAS done all of that can see exactly what an unattended
# timer invocation would look like before choosing to install it themselves.
RENEWAL_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Virtualizor Bandwidth Rollover UNATTENDED RENEWAL (writes to production -- NOT enabled by default)
After=network-online.target

[Service]
Type=oneshot
# Exit 3 is cmd_apply_due's deliberate 'WRITE_APPROVED missing, refusing by design' exit --
# declaring it here means systemd reports this run as a normal, non-failed completion
# instead of a failed unit, so a by-design safety refusal is never visually confused with a
# genuine crash in `systemctl status`/journalctl (see cmd_apply_due's own docstring).
SuccessExitStatus=3
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart=/usr/bin/flock -n {lockpath} {python} {cli} apply-due --node self --non-interactive
WorkingDirectory={workdir}
User={user}
"""

# OnBootSec=4min (two minutes after the monitor's own OnBootSec=2min) is a deliberate stagger,
# not load-bearing for correctness (the shared flock above is what actually guarantees no
# concurrent run) -- it just reduces how often the two even CONTEND for the lock in the first
# place. OnCalendar=daily was the original safe-by-default placeholder; approximately 5
# minutes is what "prompt, unattended renewal after a genuine reset" actually requires, now
# that this is a reviewed, explicitly-approved activation rather than a review-only template.
RENEWAL_TIMER_UNIT_TEMPLATE = """[Unit]
Description=Unattended renewal apply, approximately every {interval}min (writes to production once WRITE_APPROVED exists)

[Timer]
OnBootSec=4min
OnUnitActiveSec={interval}min
Unit=virt-renewal-apply.service

[Install]
WantedBy=timers.target
"""

# Readiness notifier: strictly read-only (never touches Virtualizor, never writes to the
# renewal ledger beyond its own separate notified-state/alert-log files -- see
# implementation/readiness_notifier.py). Deliberately NOT locked with TIXO_RENEWAL_LOCK_PATH:
# it only reads the SQLite ledger (WAL mode already supports concurrent readers against a
# writer) and missing a 30-minute tick under rare lock contention is harmless, unlike skipping
# a monitor poll or an apply attempt. Kept at 30 minutes -- no technical reason to change it.
NOTIFIER_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Tixo Renewal readiness notifier (read-only; reads existing monitor ledger only)
After=network-online.target

[Service]
Type=oneshot
ExecStart={python} {libdir}/readiness_notifier.py --db {workdir}/shadow-monitor-state/{db_filename} --node {node_id} --policy-config {workdir}/renewal-config/policy.json --notified-state {workdir}/shadow-monitor-state/readiness-notified.json --alert-log {workdir}/shadow-monitor-state/readiness-alerts.log
WorkingDirectory={workdir}
User={user}
"""

NOTIFIER_TIMER_UNIT_TEMPLATE = """[Unit]
Description=Run the Tixo Renewal readiness notifier every {interval} minutes (read-only, local data only)

[Timer]
OnBootSec=10min
OnUnitActiveSec={interval}min
Unit=virt-renewal-readiness-notifier.service

[Install]
WantedBy=timers.target
"""

# Inventory sync: strictly read-only (act=vs only, never managevps), entirely independent of
# the renewal engine's own lock/timer/API-budget (see inventory_sync.py's own non-overlapping
# flock on a SEPARATE lock file -- this unit intentionally does NOT share
# TIXO_RENEWAL_LOCK_PATH with monitor/apply, so a slow/locked inventory sync can never block a
# renewal apply, and vice versa). run_inventory_sync() already refuses to start a second
# overlapping run itself (status=ALREADY_RUNNING), so no external `flock` wrapper is needed
# here the way the other units use one.
INVENTORY_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Tixo Renewal VPS inventory sync (strictly read-only; independent of renewal timers)
After=network-online.target

[Service]
Type=oneshot
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} inventory-sync --node {node_id}
WorkingDirectory={workdir}
User={user}
"""

INVENTORY_TIMER_UNIT_TEMPLATE = """[Unit]
Description=Run the Tixo Renewal VPS inventory sync approximately every {interval}min

[Timer]
OnBootSec=3min
OnUnitActiveSec={interval}min
Unit=virt-renewal-inventory-sync.service

[Install]
WantedBy=timers.target
"""

# Host network accounting: reads ONLY local /sys/class/net counters -- no Virtualizor API
# call of any kind, entirely independent of inventory/renewal timers and their locks. Like
# the inventory-sync unit, this is generated for REVIEW ONLY -- never auto-enabled by
# install.sh, and in v1.7.0 specifically never installed/activated during development per
# explicit instruction (no automatic production activation). An operator who has configured
# a monitored interface (see hostnet_sync.save_monitored_interface) and reviewed this file
# enables it themselves, the same explicit pattern as every other timer in this project.
HOSTNET_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Tixo Renewal host network traffic sample (local kernel counters only, read-only)
After=network-online.target

[Service]
Type=oneshot
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} hostnet-sync
WorkingDirectory={workdir}
User={user}
"""

HOSTNET_TIMER_UNIT_TEMPLATE = """[Unit]
Description=Sample host network traffic counters approximately every {interval}min

[Timer]
OnBootSec=2min
OnUnitActiveSec={interval}min
Unit=virt-renewal-hostnet-sync.service

[Install]
WantedBy=timers.target
"""

# Optional scheduled backup: strictly LOCAL (reads/writes only under shadow-monitor-state/
# and backups/, never touches Virtualizor or any production VPS). Entirely independent of
# every renewal timer's lock. Like every optional-module unit in this project, generated-
# but-never-installed/enabled by default -- backup_scheduled_run() itself ALSO independently
# refuses to run unless backup_config.BackupScheduleSettings.enabled is True, so generating
# this file alone activates nothing. Written by its own dedicated
# write_backup_systemd_unit() rather than the shared write_systemd_units() so reconfiguring
# the backup schedule's interval can never accidentally reset an operator's previously
# customized monitor/apply/notifier/inventory/hostnet interval.
BACKUP_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Tixo Renewal scheduled backup (optional; local-only; also requires enabling via the menu)
After=network-online.target

[Service]
Type=oneshot
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} backup-scheduled-run
WorkingDirectory={workdir}
User={user}
"""

BACKUP_TIMER_UNIT_TEMPLATE = """[Unit]
Description=Run the Tixo Renewal scheduled backup approximately every {interval}h

[Timer]
OnBootSec=15min
OnUnitActiveSec={interval}h
Unit=virt-renewal-backup.service

[Install]
WantedBy=timers.target
"""

DEFAULT_BACKUP_INTERVAL_HOURS = 24


def write_backup_systemd_unit(interval_hours: int = DEFAULT_BACKUP_INTERVAL_HOURS) -> None:
    """Writes ONLY the backup service/timer pair -- deliberately separate from
    write_systemd_units() so reconfiguring the backup interval never touches any other
    timer's already-generated interval."""
    Path(SYSTEMD_OUT_DIR).mkdir(parents=True, exist_ok=True)
    python_path = sys.executable
    cli_path = os.path.abspath(__file__)
    user = os.environ.get("USER", "root")
    lib_dir = os.path.abspath(os.path.join(WORKSPACE_ROOT, "lib"))

    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-backup.service")).write_text(
        BACKUP_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                              workdir=os.path.abspath(WORKSPACE_ROOT),
                                              user=user, libdir=lib_dir))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-backup.timer")).write_text(
        BACKUP_TIMER_UNIT_TEMPLATE.format(interval=interval_hours))

# UNLIKE every other unit above, this is Type=simple/Restart=always, not a timer-triggered
# oneshot -- it holds a persistent long-poll connection to Telegram (see
# telegram_bot.run_polling_loop), so a periodic timer would be the wrong shape entirely.
# Generated-but-never-installed/enabled, exactly like every other unit here -- the bot
# itself also independently refuses to start unless explicitly enabled via the menu's
# "Configure Telegram bot" screen, so installing this unit alone does not activate anything.
TELEGRAM_BOT_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=TixoCloud Telegram bot (long-polling; optional -- also requires enabling via the menu)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} telegram-bot-run
WorkingDirectory={workdir}
User={user}

[Install]
WantedBy=multi-user.target
"""

# Same Type=simple/Restart=always shape as the Telegram bot unit above -- each of these three
# runs a persistent http.server loop (web_server.py/relay_worker.py/agent_server.py), not a
# periodic task. Generated-but-never-installed/enabled; each process also independently
# refuses to start unless explicitly enabled via its own menu "Configure..." screen, so
# installing these units alone activates nothing.
WEB_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=TixoCloud Web Control Center (optional -- also requires enabling via the menu)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} web-run
WorkingDirectory={workdir}
User={user}

[Install]
WantedBy=multi-user.target
"""

RELAY_WORKER_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=TixoCloud relay worker (optional -- runs on a SEPARATE host; also requires enabling via the menu THERE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} relay-worker-run
WorkingDirectory={workdir}
User={user}

[Install]
WantedBy=multi-user.target
"""

AGENT_SERVICE_UNIT_TEMPLATE = """[Unit]
Description=TixoCloud remote management agent (optional, read-only -- also requires enabling via the menu)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
Environment=PYTHONPATH={libdir}
Environment=TIXO_RENEWAL_INSTALL_DIR={workdir}
ExecStart={python} {cli} agent-run
WorkingDirectory={workdir}
User={user}

[Install]
WantedBy=multi-user.target
"""


# ---------- install ----------

def _node_config_path():
    # Built from the (possibly test-patched) CONFIG_DIR rather than relying on nodes.py's own
    # module-level default, which is bound once at import time and would NOT pick up a
    # monkeypatched CONFIG_DIR in tests.
    return os.path.join(CONFIG_DIR, "nodes.json")


def _policy_config_path():
    return os.path.join(CONFIG_DIR, "policy.json")


DEFAULT_MONITOR_INTERVAL_MIN = 5
DEFAULT_APPLY_INTERVAL_MIN = 5
DEFAULT_NOTIFIER_INTERVAL_MIN = 30
DEFAULT_INVENTORY_INTERVAL_MIN = 15


DEFAULT_HOSTNET_INTERVAL_MIN = 5


def write_systemd_units(monitor_interval_min: int = DEFAULT_MONITOR_INTERVAL_MIN,
                         apply_interval_min: int = DEFAULT_APPLY_INTERVAL_MIN,
                         notifier_interval_min: int = DEFAULT_NOTIFIER_INTERVAL_MIN,
                         inventory_interval_min: int = DEFAULT_INVENTORY_INTERVAL_MIN,
                         hostnet_interval_min: int = DEFAULT_HOSTNET_INTERVAL_MIN) -> None:
    """Writes all 6 review-only systemd unit files to SYSTEMD_OUT_DIR, parameterized by the
    given intervals (each validated/clamped by the caller via validate_interval_minutes
    BEFORE being passed here -- this function trusts its inputs are already safe ints, never
    interpolates a raw operator string itself). Shared by cmd_install (install-time defaults)
    and the interactive menu (interval reconfiguration) so there is exactly one code path that
    ever generates these files -- never two divergent implementations to keep in sync."""
    Path(SYSTEMD_OUT_DIR).mkdir(parents=True, exist_ok=True)
    python_path = sys.executable
    cli_path = os.path.abspath(__file__)
    user = os.environ.get("USER", "root")
    lib_dir = os.path.abspath(os.path.join(WORKSPACE_ROOT, "lib"))

    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-monitor.service")).write_text(
        SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                      workdir=os.path.abspath(WORKSPACE_ROOT), user=user,
                                      libdir=lib_dir, lockpath=TIXO_RENEWAL_LOCK_PATH))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer")).write_text(
        TIMER_UNIT_TEMPLATE.format(interval=monitor_interval_min))

    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-apply.service")).write_text(
        RENEWAL_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                              workdir=os.path.abspath(WORKSPACE_ROOT), user=user,
                                              libdir=lib_dir, lockpath=TIXO_RENEWAL_LOCK_PATH))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-apply.timer")).write_text(
        RENEWAL_TIMER_UNIT_TEMPLATE.format(interval=apply_interval_min))

    first_node = nodes_mod.load_nodes(_node_config_path())[0]
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-readiness-notifier.service")).write_text(
        NOTIFIER_SERVICE_UNIT_TEMPLATE.format(python=python_path, workdir=os.path.abspath(WORKSPACE_ROOT),
                                               user=user, libdir=lib_dir, node_id=first_node.node_id,
                                               db_filename=first_node.db_filename))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-readiness-notifier.timer")).write_text(
        NOTIFIER_TIMER_UNIT_TEMPLATE.format(interval=notifier_interval_min))

    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-inventory-sync.service")).write_text(
        INVENTORY_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                                workdir=os.path.abspath(WORKSPACE_ROOT), user=user,
                                                libdir=lib_dir, node_id=first_node.node_id))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-inventory-sync.timer")).write_text(
        INVENTORY_TIMER_UNIT_TEMPLATE.format(interval=inventory_interval_min))

    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-hostnet-sync.service")).write_text(
        HOSTNET_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                              workdir=os.path.abspath(WORKSPACE_ROOT), user=user,
                                              libdir=lib_dir))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-hostnet-sync.timer")).write_text(
        HOSTNET_TIMER_UNIT_TEMPLATE.format(interval=hostnet_interval_min))

    # No .timer counterpart -- see TELEGRAM_BOT_SERVICE_UNIT_TEMPLATE's docstring-comment:
    # this is a persistent long-poll process, not a periodic task.
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-telegram-bot.service")).write_text(
        TELEGRAM_BOT_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                                   workdir=os.path.abspath(WORKSPACE_ROOT),
                                                   user=user, libdir=lib_dir))

    # Same "generated but never installed/enabled" discipline as every unit above.
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-web.service")).write_text(
        WEB_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                           workdir=os.path.abspath(WORKSPACE_ROOT),
                                           user=user, libdir=lib_dir))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-relay-worker.service")).write_text(
        RELAY_WORKER_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                                    workdir=os.path.abspath(WORKSPACE_ROOT),
                                                    user=user, libdir=lib_dir))
    Path(os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-agent.service")).write_text(
        AGENT_SERVICE_UNIT_TEMPLATE.format(python=python_path, cli=cli_path,
                                             workdir=os.path.abspath(WORKSPACE_ROOT),
                                             user=user, libdir=lib_dir))


def cmd_install(args):
    for d in (STATE_DIR, CONFIG_DIR, BACKUP_DIR, SYSTEMD_OUT_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
    # Host-local deployment (TIXO_RENEWAL_INSTALL_DIR set by deploy/install.sh) defaults to a
    # single mode="local" node representing THIS host -- never the dev-server's SSH-to-
    # production node list. Idempotent either way: a config file that already exists is left
    # untouched by write_default_config().
    is_host_local = bool(os.environ.get("TIXO_RENEWAL_INSTALL_DIR"))
    default_nodes = [nodes_mod.LOCAL_DEPLOYMENT_NODE_TEMPLATE] if is_host_local else None
    nodes_mod.write_default_config(_node_config_path(), nodes=default_nodes)  # no-op if already present
    # Monthly base is configurable AT install time via env var, so an operator doesn't have to
    # hand-edit policy.json immediately after a fresh install -- still just the same editable
    # file afterward either way (write_default_policy_config is a no-op if it already exists).
    monthly_base_env = os.environ.get("TIXO_RENEWAL_MONTHLY_BASE_GB")
    monthly_base_gb = Decimal(monthly_base_env) if monthly_base_env else None
    pol.write_default_policy_config(_policy_config_path(), initial_allocation_gb=monthly_base_gb,
                                     monthly_grant_gb=monthly_base_gb)  # idempotent; auto_renewal_enabled=False
    for node in nodes_mod.load_nodes(_node_config_path()):
        sm.open_db(node.db_path(STATE_DIR)).close()  # idempotent schema creation

    # Intervals are configurable AT install time via env var, same convention as the monthly
    # gift -- validated/clamped before ever reaching a template.
    monitor_min = validate_interval_minutes(os.environ.get("TIXO_RENEWAL_MONITOR_INTERVAL_MIN", DEFAULT_MONITOR_INTERVAL_MIN))
    apply_min = validate_interval_minutes(os.environ.get("TIXO_RENEWAL_APPLY_INTERVAL_MIN", DEFAULT_APPLY_INTERVAL_MIN))
    notifier_min = validate_interval_minutes(os.environ.get("TIXO_RENEWAL_NOTIFIER_INTERVAL_MIN", DEFAULT_NOTIFIER_INTERVAL_MIN))
    inventory_min = validate_interval_minutes(os.environ.get("TIXO_RENEWAL_INVENTORY_INTERVAL_MIN", DEFAULT_INVENTORY_INTERVAL_MIN))
    hostnet_min = validate_interval_minutes(os.environ.get("TIXO_RENEWAL_HOSTNET_INTERVAL_MIN", DEFAULT_HOSTNET_INTERVAL_MIN))
    write_systemd_units(monitor_min, apply_min, notifier_min, inventory_min, hostnet_min)
    write_backup_systemd_unit()
    service_path = os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-monitor.service")
    timer_path = os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer")

    # v1.10.7: (re)write the global `tixo-renewal` wrapper HERE, in Python, on EVERY call --
    # fresh install AND every upgrade path (cmd_install_upgrade/the menu install
    # wizard/update-apply all call installer.upgrade(), which itself calls this same
    # cmd_install()) -- so the wrapper can never again go stale relative to the actual
    # installed code the way it did in the real, confirmed v1.10.5->v1.10.6 production
    # regression on ir1. Only attempted for a real host-local deployment (matching the same
    # is_host_local gate the node-config default above already uses) -- never for the
    # dev-server's own multi-node SSH setup.
    wrapper_result = {"written": False, "reason": "not a host-local deployment"}
    wrapper_verify = None
    if is_host_local:
        import installer as inst
        wrapper_result = inst.write_global_wrapper(WORKSPACE_ROOT)
        # v1.10.9: don't just WRITE the wrapper and trust it -- actually RUN it and confirm
        # real command dispatch works, so this function can refuse to report success on the
        # exact class of bug that shipped as "fixed" twice before (v1.10.7, v1.10.8) and was
        # only ever caught by a real post-hoc production report, never by this installer
        # itself.
        if wrapper_result["written"]:
            wrapper_verify = inst.verify_wrapper_dispatch(wrapper_result["path"], VERSION)

    print("Install complete (idempotent -- safe to re-run).")
    print(f"  State dir:   {STATE_DIR}")
    print(f"  Node config: {os.path.join(CONFIG_DIR, 'nodes.json')}")
    print(f"  Systemd units written to: {SYSTEMD_OUT_DIR}")
    print("\nThese units only ever run `dry-run` (read-only). To activate the periodic timer,")
    print("review the unit files, then as an explicit operator action:")
    print(f"  sudo cp {service_path} {timer_path} /etc/systemd/system/")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now virt-renewal-monitor.timer")
    print("\nWrite capability (applying a renewal to production) remains BLOCKED until you")
    print(f"deliberately create: {WRITE_APPROVAL_MARKER}")
    print(f"\nPolicy config: {_policy_config_path()} (edit this to set per-host/per-VPS "
          f"bandwidth policies; `policy-show`/`policy-validate` to review).")
    print(f"\nAn UNATTENDED renewal unit was also generated for review ONLY (never copied or "
          f"enabled by this installer): {os.path.join(SYSTEMD_OUT_DIR, 'virt-renewal-apply.service')}")
    print("It still refuses to run without WRITE_APPROVED, and `store-credentials` must be run")
    print("by you first -- no credentials are ever created or stored automatically.")

    install_ok = True
    if wrapper_result["written"]:
        if wrapper_verify and wrapper_verify["ok"]:
            print(f"\nGlobal command (re)installed and VERIFIED: {wrapper_result['path']}")
            print("  tixo-renewal           -> interactive menu")
            print("  tixo-renewal <command> -> any renewal_cli.py subcommand, e.g. 'status', "
                  "'doctor', 'backup' -- PYTHONPATH/TIXO_RENEWAL_INSTALL_DIR already set")
        else:
            install_ok = False
            print(f"\n! INSTALLATION FAILED: the global command at {wrapper_result['path']} "
                  f"was written but does NOT actually dispatch commands correctly.")
            print(f"  --version check: {'OK' if wrapper_verify and wrapper_verify.get('version_ok') else 'FAILED'}"
                  f" ({wrapper_verify.get('version_output', '') if wrapper_verify else wrapper_verify})")
            print(f"  install-detect check: "
                  f"{'OK' if wrapper_verify and wrapper_verify.get('detect_ok') else 'FAILED'}"
                  f" ({wrapper_verify.get('detect_output', '') if wrapper_verify else ''})")
            print(f"  Use the full path directly instead until this is resolved:")
            print(f"  TIXO_RENEWAL_INSTALL_DIR={WORKSPACE_ROOT} "
                  f"PYTHONPATH={WORKSPACE_ROOT}/lib:{WORKSPACE_ROOT}/bin "
                  f"python3 {WORKSPACE_ROOT}/bin/renewal_cli.py status")
    elif is_host_local:
        print(f"\nCould not (re)install the global 'tixo-renewal' command: "
              f"{wrapper_result['reason']}. Run every command with the full path and "
              f"PYTHONPATH set manually, e.g.:")
        print(f"  TIXO_RENEWAL_INSTALL_DIR={WORKSPACE_ROOT} "
              f"PYTHONPATH={WORKSPACE_ROOT}/lib:{WORKSPACE_ROOT}/bin "
              f"python3 {WORKSPACE_ROOT}/bin/renewal_cli.py status")

    import installer as inst
    legacy = inst.detect_legacy_bandwidth_automation()
    if legacy["found_active"] or legacy["found_disabled"]:
        print(f"\nNOTE: found existing, foreign bandwidth-automation cron job(s) on this "
              f"host, NOT installed by this project and NOT touched by this installer:")
        if legacy["found_active"]:
            print(f"  Active:   {legacy['found_active']}")
        if legacy["found_disabled"]:
            print(f"  Disabled: {legacy['found_disabled']}")
        if legacy["active_write_capable"]:
            print(f"  ! At least one ACTIVE job appears write-capable -- "
                  f"'enable-auto-renewal' will refuse to run alongside it unless you pass "
                  f"--override-legacy-write-conflict after confirming this is intentional.")

    return 0 if install_ok else 1


# ---------- policy ----------

def cmd_policy_show(args):
    cfg = pol.load_policy_config(_policy_config_path())
    print("Global defaults:")
    for f in ("initial_allocation_gb", "monthly_grant_gb", "carryover_enabled",
              "carryover_cap_gb", "billing_anchor", "auto_renewal_enabled"):
        print(f"  {f}: {getattr(cfg.global_defaults, f)}")
    print(f"\nHost overrides ({len(cfg.host_overrides)}):")
    for host_id, ov in cfg.host_overrides.items():
        print(f"  {host_id}: {vars(ov)}")
    print(f"\nVPS overrides ({len(cfg.vps_overrides)}):")
    for key, ov in cfg.vps_overrides.items():
        print(f"  {key}: {vars(ov)}")


def cmd_policy_validate(args):
    try:
        pol.load_policy_config(_policy_config_path())
        print(f"OK: {_policy_config_path()} is valid.")
        return 0
    except pol.PolicyValidationError as e:
        print(f"INVALID: {e}")
        return 1
    except Exception as e:
        print(f"ERROR reading policy config: {e}")
        return 1


def _renewal_baseline_for_vpsid(conn, vpsid):
    """Returns (old_quota, old_used, event_ts) for a renewal computation.

    If the two most recent snapshots show the reset PATTERN (quota unchanged, usage dropped),
    the correct "pre-renewal" baseline is the OLDER of the two snapshots (quota/used as they
    stood right before the drop) -- using only the single latest row here would silently use
    the ALREADY-POST-DROP usage as if it were the pre-renewal usage, producing a wrong
    (too-small remaining, too-small proposed quota) result. This exact bug was caught by a
    real end-to-end test, not just by code review.

    If no drop is observed yet (still accumulating usage), there is nothing pending to apply;
    callers that need a hypothetical "what if we renewed right now" preview should use the
    single latest row directly -- this function returns None in that case so callers can
    choose their own fallback deliberately rather than silently reusing the wrong value.
    """
    from decimal import Decimal
    rows = list(conn.execute(
        "SELECT captured_at, quota_gb, used_gb FROM snapshots WHERE vpsid=? "
        "ORDER BY captured_at DESC LIMIT 2", (vpsid,)
    ))
    if len(rows) < 2:
        return None
    (curr_ts, curr_q, curr_u), (prev_ts, prev_q, prev_u) = rows
    if Decimal(curr_q) == Decimal(prev_q) and Decimal(curr_u) < Decimal(prev_u):
        return Decimal(prev_q), Decimal(prev_u), curr_ts
    return None


def cmd_preview(args):
    cfg = pol.load_policy_config(_policy_config_path())
    node = next((n for n in nodes_mod.load_nodes(_node_config_path()) if n.node_id == args.node), None)
    if node is None:
        print(f"Unknown node: {args.node}")
        return 1
    db_path = node.db_path(STATE_DIR)
    if not os.path.exists(db_path):
        print(f"No data yet for node {args.node} -- run `dry-run` first.")
        return 1
    conn = sm.open_db(db_path)
    from decimal import Decimal
    baseline = _renewal_baseline_for_vpsid(conn, args.vpsid)
    if baseline is not None:
        old_quota, old_used, event_ts = baseline
        print("(A reset pattern was already observed -- previewing using the pre-drop "
              "snapshot as the baseline, matching what `apply-due` would actually use.)")
    else:
        row = conn.execute("SELECT quota_gb, used_gb FROM snapshots WHERE vpsid=? "
                            "ORDER BY captured_at DESC LIMIT 1", (args.vpsid,)).fetchone()
        if row is None:
            print(f"No snapshot yet for vpsid={args.vpsid} on node {args.node}.")
            conn.close()
            return 1
        old_quota, old_used, event_ts = Decimal(row[0]), Decimal(row[1]), sm.utcnow_iso()
        print("(No reset observed yet -- this is a HYPOTHETICAL preview using today's "
              "current quota/usage as if a renewal happened right now.)")
    ident = conn.execute("SELECT created_epoch FROM identities WHERE vpsid=?", (args.vpsid,)).fetchone()
    checkpoint = conn.execute("SELECT first_poll_at FROM monitor_checkpoints WHERE id=1").fetchone()
    conn.close()
    result = pi.preview_renewal(
        cfg, args.node, args.vpsid,
        verified_created_epoch=ident[0] if ident else None,
        node_first_poll_at=checkpoint[0] if checkpoint else None,
        old_quota=old_quota, old_used=old_used, event_ts=event_ts,
    )
    print(f"=== Renewal preview: node={args.node} vpsid={args.vpsid} [PREVIEW ONLY, NOT APPLIED] ===")
    for k, v in result.items():
        print(f"  {k}: {v}")


# ---------- status ----------

def cmd_status(args):
    # Regression fix: this previously called nodes_mod.load_nodes() with NO path, which binds
    # to nodes.py's own module-level default (the DEV workspace's renewal-config/nodes.json),
    # NOT this install's (possibly test-patched or host-local) CONFIG_DIR -- every other
    # subcommand already goes through _node_config_path() for exactly this reason. Had zero
    # test coverage, so this silently diverged undetected.
    policy_config = None
    if os.path.exists(_policy_config_path()):
        try:
            policy_config = pol.load_policy_config(_policy_config_path())
        except pol.PolicyValidationError:
            policy_config = None  # status must never crash on a malformed policy file
    for node in nodes_mod.load_nodes(_node_config_path()):
        print(f"=== Node: {node.node_id} ({node.ssh_host}) ===")
        db_path = node.db_path(STATE_DIR)
        if not os.path.exists(db_path):
            print("  Not yet initialized -- run `install` then `dry-run`.\n")
            continue
        conn = sm.open_db(db_path)
        checkpoint = conn.execute(
            "SELECT last_poll_at, last_poll_row_count FROM monitor_checkpoints WHERE id=1"
        ).fetchone()
        print(f"  Last poll: {checkpoint[0] if checkpoint else 'never'}")
        print(f"  Identities tracked: {conn.execute('SELECT COUNT(*) FROM identities').fetchone()[0]}")
        print(f"  Identity conflicts: {conn.execute('SELECT COUNT(*) FROM identity_conflicts').fetchone()[0]}")
        print(f"  Renewal periods on record: {conn.execute('SELECT COUNT(*) FROM renewal_checkpoints').fetchone()[0]}")
        detections = sm.detect_renewal_candidates(conn, node_id=node.node_id, policy_config=policy_config)
        pending = [d for d in detections if d["status"] == "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION"]
        frozen = [d for d in detections if d["status"] == "FROZEN_FOR_REVIEW"]
        manual_unlimited = [d for d in detections if d["status"] == "MANUAL_ONLY_UNLIMITED"]
        print(f"  Plausible candidates awaiting operator confirmation: {len(pending)}")
        print(f"  Frozen-for-review entries: {len(frozen)}")
        print(f"  Manual-only (unlimited bandwidth, permanently excluded): {len(manual_unlimited)}")
        write_blocked = not os.path.exists(WRITE_APPROVAL_MARKER)
        print(f"  Write capability: {'BLOCKED (no approval marker)' if write_blocked else 'APPROVED -- apply command enabled'}")
        conn.close()
        print()


# ---------- dry-run ----------

def _fetch_rows_for_node(node):
    """Dispatches on node.mode: 'ssh' polls a remote host from here (dev-server multi-node
    poller); 'local' reads this host's own Virtualizor DB directly (host-local deployment,
    no SSH, no dependency on any other server)."""
    if node.mode == "local":
        import local_source as ls
        return ls.fetch_live_rows_local()
    return sm.fetch_live_rows(node.ssh_cmd())


def cmd_dry_run(args):
    policy_config = None
    if os.path.exists(_policy_config_path()):
        try:
            policy_config = pol.load_policy_config(_policy_config_path())
        except pol.PolicyValidationError:
            policy_config = None  # dry-run must never crash on a malformed policy file
    for node in nodes_mod.load_nodes(_node_config_path()):
        label = node.ssh_host if node.mode == "ssh" else "this host, local DB"
        print(f"=== Dry-run poll: {node.node_id} ({label}) [READ-ONLY] ===")
        conn = sm.open_db(node.db_path(STATE_DIR))
        try:
            rows = _fetch_rows_for_node(node)
        except Exception as e:
            print(f"  Read-only fetch FAILED: {e}")
            conn.close()
            continue
        as_of = sm.utcnow_iso()
        result = sm.ingest_poll(conn, rows, as_of)
        print(f"  Ingested {result['rows_ingested']} rows ({len(result['identity_conflicts'])} identity conflicts)")
        detections = sm.detect_renewal_candidates(conn, node_id=node.node_id, policy_config=policy_config)
        sm.print_report(conn, detections)
        node_alerts = alerts.compute_alerts(conn, detections)
        print()
        print(alerts.format_alerts(node_alerts))
        conn.close()
        print()


# ---------- verify ----------

def cmd_verify(args):
    print("=== Running test suite ===")
    tests_dir = os.path.join(WORKSPACE_ROOT, "tests")
    result = subprocess.run([sys.executable, "-m", "pytest", tests_dir, "-q"],
                             cwd=WORKSPACE_ROOT)
    tests_ok = result.returncode == 0

    print("\n=== Live read-only reachability check per node ===")
    all_reachable = True
    for node in nodes_mod.load_nodes(_node_config_path()):
        try:
            rows = _fetch_rows_for_node(node)
            print(f"  {node.node_id}: OK ({len(rows)} VPS visible, read-only)")
        except Exception as e:
            print(f"  {node.node_id}: UNREACHABLE -- {e}")
            all_reachable = False

    print(f"\nVerify result: tests_passed={tests_ok}, all_nodes_reachable={all_reachable}")
    return 0 if (tests_ok and all_reachable) else 1


# ---------- backup / rollback ----------

def cmd_backup(args):
    # Same regression class fixed in cmd_status: must use THIS install's node config path,
    # never nodes.py's own module-level default.
    for node in nodes_mod.load_nodes(_node_config_path()):
        db_path = node.db_path(STATE_DIR)
        if not os.path.exists(db_path):
            print(f"{node.node_id}: no database yet, skipping")
            continue
        dest = backup_mod.backup_db(db_path, BACKUP_DIR, label=node.node_id)
        print(f"{node.node_id}: backed up -> {dest}")


def cmd_backup_full(args):
    """Builds one archived, integrity-verified, retention-pruned backup covering all known
    state databases plus allowlisted non-secret config (see backup.py). Optional --encrypt
    passphrase is read from the TIXO_BACKUP_PASSPHRASE env var, never a CLI argument (which
    `ps` could reveal). Every action is recorded to the audit log."""
    import audit_log as alog

    archive = backup_mod.create_full_backup_archive(STATE_DIR, CONFIG_DIR, BACKUP_DIR,
                                                      label=args.label)
    verification = backup_mod.verify_backup_archive(archive)
    if not verification["ok"]:
        print(f"WARNING: backup archive failed integrity verification: {verification}")

    final_path = archive
    if args.encrypt:
        passphrase = os.environ.get("TIXO_BACKUP_PASSPHRASE")
        if not passphrase:
            print("ERROR: --encrypt requires TIXO_BACKUP_PASSPHRASE to be set in the "
                  "environment (never pass a passphrase as a CLI argument).")
            return 1
        try:
            final_path = backup_mod.encrypt_backup_file(archive, passphrase)
        except backup_mod.BackupEncryptionUnavailable as exc:
            print(f"ERROR: {exc}")
            return 1
        os.remove(archive)

    removed = []
    if args.keep:
        removed = backup_mod.apply_retention(BACKUP_DIR, keep_count=args.keep)

    conn = alog.open_db(os.path.join(STATE_DIR, "audit_log.sqlite3"))
    try:
        alog.record_event(conn, actor="cli", action="backup_full_created",
                           detail=f"archive={os.path.basename(final_path)} "
                                  f"encrypted={args.encrypt} integrity_ok={verification['ok']} "
                                  f"retention_removed={len(removed)}",
                           remote_addr="local")
    finally:
        conn.close()

    print(f"Backup archive created: {final_path}")
    print(f"Integrity: {'ok' if verification['ok'] else verification}")
    if removed:
        print(f"Retention removed {len(removed)} older archive(s): {removed}")
    return 0 if verification["ok"] else 1


def cmd_backup_scheduled_run(args):
    """The ENTRY POINT for the optional `virt-renewal-backup.timer` -- unlike `backup-full`
    (manual/CLI, passphrase from env var), this reads its behavior entirely from
    backup_config.BackupScheduleSettings/the stored passphrase file, since an unattended
    timer has no interactive terminal and no env var to read. Refuses to run unless
    explicitly enabled, same pattern as every other optional-module `*-run` command."""
    import audit_log as alog
    import backup_config as bcfg

    settings = bcfg.load_backup_schedule_settings(CONFIG_DIR)
    if not settings.enabled:
        print("BLOCKED: Scheduled backup is not enabled. Enable it via the menu's "
              "'Configure Backup Schedule' screen first.")
        return 1

    conn = alog.open_db(os.path.join(STATE_DIR, "audit_log.sqlite3"))
    try:
        archive = backup_mod.create_full_backup_archive(STATE_DIR, CONFIG_DIR, BACKUP_DIR,
                                                           label="scheduled")
        verification = backup_mod.verify_backup_archive(archive)
        if not verification["ok"]:
            print(f"WARNING: backup archive failed integrity verification: {verification}")

        final_path = archive
        if settings.encrypt:
            try:
                passphrase = bcfg.load_backup_passphrase(bcfg.default_passphrase_path(CONFIG_DIR))
            except (FileNotFoundError, bcfg.BackupScheduleSecurityError) as e:
                print(f"ERROR: {e}")
                alog.record_event(conn, actor="scheduled-backup", action="backup_failed",
                                   detail=str(e), remote_addr="local")
                return 1
            try:
                final_path = backup_mod.encrypt_backup_file(archive, passphrase)
            except backup_mod.BackupEncryptionUnavailable as exc:
                print(f"ERROR: {exc}")
                alog.record_event(conn, actor="scheduled-backup", action="backup_failed",
                                   detail=str(exc), remote_addr="local")
                return 1
            os.remove(archive)

        removed = backup_mod.apply_retention(BACKUP_DIR, keep_count=settings.retention_keep)
        alog.record_event(conn, actor="scheduled-backup", action="backup_full_created",
                           detail=f"archive={os.path.basename(final_path)} "
                                  f"encrypted={settings.encrypt} integrity_ok={verification['ok']} "
                                  f"retention_removed={len(removed)}",
                           remote_addr="local")
        print(f"Scheduled backup created: {final_path}")
        if removed:
            print(f"Retention removed {len(removed)} older archive(s): {removed}")

        if settings.telegram_delivery_enabled:
            import telegram_config as tgc
            import telegram_bot as tbot
            tg_settings = tgc.load_telegram_settings(CONFIG_DIR)
            if not tg_settings.enabled or not tg_settings.admin_ids:
                print("Telegram delivery skipped: bot not enabled or no admins configured.")
            else:
                try:
                    secrets_obj = tgc.load_telegram_secrets(tgc.default_secrets_path(CONFIG_DIR))
                    transport = tbot.build_transport(tg_settings, secrets_obj)
                    delivery = backup_mod.send_backup_to_telegram(
                        transport, tg_settings.admin_ids, final_path)
                    alog.record_event(conn, actor="scheduled-backup",
                                       action="backup_telegram_delivery",
                                       detail=json.dumps({k: v["ok"] for k, v in delivery.items()}),
                                       remote_addr="local")
                    for admin_id, result in delivery.items():
                        status = "delivered" if result["ok"] else f"FAILED ({result['error']})"
                        print(f"  Telegram delivery to admin {admin_id}: {status}")
                except (backup_mod.BackupDeliverySecurityError, ValueError,
                        FileNotFoundError, tgc.TelegramConfigSecurityError) as e:
                    print(f"Telegram delivery blocked: {e}")
                    alog.record_event(conn, actor="scheduled-backup",
                                       action="backup_telegram_delivery_blocked",
                                       detail=str(e), remote_addr="local")

        return 0 if verification["ok"] else 1
    finally:
        conn.close()


def cmd_rollback(args):
    if not args.backup_file:
        print("Available backups:")
        for f in backup_mod.list_backups(BACKUP_DIR):
            print(f"  {f}")
        print("\nUsage: tixo-renewal rollback --node <node_id> --backup-file <filename> --yes")
        return 1
    node = next((n for n in nodes_mod.load_nodes(_node_config_path()) if n.node_id == args.node), None)
    if node is None:
        print(f"Unknown node: {args.node}")
        return 1
    if not args.yes:
        print(f"This will OVERWRITE the local ledger for node '{args.node}' with "
              f"{args.backup_file}. A safety copy of the current state is made first. "
              f"Re-run with --yes to confirm.")
        return 1
    backup_path = os.path.join(BACKUP_DIR, args.backup_file)
    target = node.db_path(STATE_DIR)
    backup_mod.restore_db(backup_path, target)
    print(f"Restored {args.backup_file} -> {target} (previous state saved as {target}.pre-restore.sqlite3)")


# ---------- unified installer: detect / upgrade / repair / rollback -----------------------------

def cmd_install_detect(args):
    """Read-only: reports whether this looks like a fresh or existing install, without
    changing anything. The first step any installer flow (interactive or noninteractive)
    should take before choosing fresh-install vs. upgrade/repair."""
    import installer as inst
    result = inst.detect_installation_state(CONFIG_DIR, STATE_DIR)
    print(json.dumps(result, indent=2))
    return 0


def cmd_install_upgrade(args):
    """Backs up config/state first, then re-runs the idempotent install steps and
    regenerates every systemd unit template. Never overwrites an existing nodes.json/
    policy.json -- preserves all existing Virtualizor renewal configuration."""
    import installer as inst
    result = inst.upgrade(CONFIG_DIR, STATE_DIR, BACKUP_DIR)
    print(f"Upgrade {'succeeded' if result['ok'] else 'FAILED'}.")
    print(f"Pre-upgrade backup: {result['backup']}")
    return 0 if result["ok"] else 1


def cmd_install_repair(args):
    """Recreates missing directories/files/schemas and fixes any loosened secret-file
    permission, after first backing up. Never touches anything that already exists and is
    valid."""
    import installer as inst
    result = inst.repair(CONFIG_DIR, STATE_DIR, BACKUP_DIR)
    print(f"Repair {'succeeded' if result['ok'] else 'FAILED'}.")
    print(f"Pre-repair backup: {result['backup']}")
    if result["fixed_permissions"]:
        print(f"Fixed loosened permissions on: {result['fixed_permissions']}")
    else:
        print("No permission issues found.")
    return 0 if result["ok"] else 1


def cmd_install_rollback(args):
    """Restores config/state from a full backup archive -- an explicit recovery action for
    a FAILED upgrade/repair. Safety-copies the current state first; refuses (raises) a
    failed-integrity-check archive before touching anything live."""
    if not args.yes:
        print(f"This will OVERWRITE live config/state from {args.backup_file}. The CURRENT "
              f"state is safety-copied first. Re-run with --yes to confirm.")
        return 1
    import installer as inst
    archive_path = os.path.join(BACKUP_DIR, args.backup_file)
    try:
        result = inst.rollback(archive_path, CONFIG_DIR, STATE_DIR)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Restored databases: {result['restored_databases']}")
    print(f"Restored config: {result['restored_config']}")
    print(f"Safety backup of pre-rollback state: {result['safety_backup']}")
    return 0


def cmd_uninstall_module(args):
    """Safely disables ONE optional module (Telegram/Web/relay worker/remote agent/backup
    schedule) without touching the core renewal install or any other module. Secrets are
    kept unless --purge-secrets is explicitly passed."""
    import installer as inst
    if args.module not in inst.OPTIONAL_MODULES:
        print(f"ERROR: unknown module {args.module!r} (must be one of {inst.OPTIONAL_MODULES})")
        return 1
    result = inst.uninstall_optional_module(args.module, CONFIG_DIR, SYSTEMD_OUT_DIR,
                                              purge_secrets=args.purge_secrets)
    print(f"Module {result['module']!r} disabled.")
    if result["secrets_purged"]:
        print("Secrets purged.")
    if result["units_removed"]:
        print(f"Removed generated systemd unit file(s): {result['units_removed']}")
    if "systemd_disabled" in result:
        if result["systemd_disabled"]:
            print("Real systemd timer stopped and disabled (verified).")
        else:
            print(f"WARNING: could not stop/disable the real systemd timer: "
                  f"{result.get('systemd_problems')} -- it may still be running. "
                  f"Re-run as root.")
    elif result["units_removed"]:
        print("If any were ever installed into /etc/systemd/system, disable/remove them "
              "there yourself (a separate explicit root action).")
    return 0


def cmd_install_health_check(args):
    """Read-only post-change sanity check -- never restarts or enables anything. As of
    v1.10.3 this also verifies the real systemd state of every one of this project's own
    timers (enabled/active/failed), not just that the config files parse -- see
    installer.health_check's docstring for why that extra check is opt-in here and not in
    the updater's own automatic rollback gate."""
    import installer as inst
    result = inst.health_check(CONFIG_DIR, STATE_DIR, check_systemd=True)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


# ---------- readiness / doctor / logs (v1.10.6: unified operational visibility) -----------------

# A VPS whose only "problem" is still accumulating its first two observations is NOT a
# financial-accounting concern -- it is normal, self-resolving first-run behavior (every
# fresh install starts here; the next poll clears it automatically). Everything else
# detect_renewal_candidates() can report IS a genuine thing an operator should look at
# (identity conflict, a too-wide observation gap, or an actual candidate awaiting
# confirmation). This mapping is the ONLY new classification logic here -- the underlying
# per-VPS facts all come from the existing, already-tested shadow_monitor/engine state
# machine, never reimplemented.
READINESS_COLLECTING_BASELINE = "COLLECTING_BASELINE"
READINESS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
READINESS_RENEWAL_CANDIDATE_PENDING = "RENEWAL_CANDIDATE_PENDING_REVIEW"
READINESS_MANUAL_ONLY_UNLIMITED = "MANUAL_ONLY_UNLIMITED"
READINESS_MONITORING = "MONITORING"


def _per_vps_readiness(conn, node_id, policy_config) -> dict:
    """Read-only. Returns {vpsid: readiness_bucket} for every vpsid this node has ever
    recorded a snapshot for, by calling the existing, already-tested
    shadow_monitor.detect_renewal_candidates() once and reconciling it against raw snapshot
    counts -- never re-deriving the actual financial logic here."""
    all_vpsids = [r[0] for r in conn.execute("SELECT DISTINCT vpsid FROM snapshots")]
    detections = {d["vpsid"]: d for d in sm.detect_renewal_candidates(
        conn, node_id=node_id, policy_config=policy_config)}
    buckets = {}
    for vpsid in all_vpsids:
        count = conn.execute("SELECT COUNT(*) FROM snapshots WHERE vpsid=?", (vpsid,)).fetchone()[0]
        d = detections.get(vpsid)
        if d is None:
            buckets[vpsid] = READINESS_MONITORING if count >= 2 else READINESS_COLLECTING_BASELINE
        elif d["status"] == "MANUAL_ONLY_UNLIMITED":
            buckets[vpsid] = READINESS_MANUAL_ONLY_UNLIMITED
        elif d["status"] == "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION":
            buckets[vpsid] = READINESS_RENEWAL_CANDIDATE_PENDING
        elif "fewer than two observations" in d.get("reason", ""):
            buckets[vpsid] = READINESS_COLLECTING_BASELINE
        elif d.get("engine_kind") in ("NOT_A_RENEWAL", "DUPLICATE_RENEWAL_PERIOD"):
            # NOT_A_RENEWAL: usage simply grew (or stayed flat) -- the single most common
            # steady-state case in real production data, and not a financial-accounting
            # concern. DUPLICATE_RENEWAL_PERIOD: the engine's own duplicate-renewal guard
            # correctly rejected a second grant for an already-applied period -- this is the
            # protection working exactly as intended, never something to flag for review.
            buckets[vpsid] = READINESS_MONITORING
        else:
            # Covers UNVERIFIED_ELIGIBILITY (a genuinely reset-shaped pattern whose calendar
            # eligibility could not be confirmed) and any identity-conflict/gap-detected
            # case handled earlier in this same function's own direct checks -- these ARE
            # worth a human glance, per the engine's own fail-closed design intent.
            buckets[vpsid] = READINESS_REVIEW_REQUIRED
    return buckets


def cmd_readiness(args):
    """Read-only operational-readiness summary, distinct from install-health-check's
    service-level health: this answers 'is the financial engine actually ready to monitor/
    renew correctly', not just 'did the files parse and are the timers running'. A node can
    be perfectly healthy at the service level while every single VPS is still
    COLLECTING_BASELINE (normal immediately after a fresh install) -- that must never be
    confused with a service failure, and must never be silently hidden either."""
    import installer as inst
    overall = {"ok": True, "nodes": {}}

    policy_config = None
    if os.path.exists(_policy_config_path()):
        try:
            policy_config = pol.load_policy_config(_policy_config_path())
        except pol.PolicyValidationError:
            policy_config = None

    write_approved = os.path.exists(WRITE_APPROVAL_MARKER)
    auto_renewal_enabled = policy_config.global_defaults.auto_renewal_enabled if policy_config else False
    overall["writer_approved"] = write_approved
    overall["auto_renewal_enabled"] = auto_renewal_enabled
    overall["legacy_bandwidth_automation"] = inst.detect_legacy_bandwidth_automation()

    for node in nodes_mod.load_nodes(_node_config_path()):
        db_path = node.db_path(STATE_DIR)
        node_report = {"state": "INITIALIZING", "vps_total": 0, "buckets": {}, "vps": {}}
        if os.path.exists(db_path):
            conn = sm.open_db(db_path)
            buckets = _per_vps_readiness(conn, node.node_id, policy_config)
            conn.close()
            node_report["vps"] = buckets
            node_report["vps_total"] = len(buckets)
            for bucket in buckets.values():
                node_report["buckets"][bucket] = node_report["buckets"].get(bucket, 0) + 1
            if not buckets:
                node_report["state"] = "INITIALIZING"
            elif node_report["buckets"].get(READINESS_REVIEW_REQUIRED, 0) > 0:
                node_report["state"] = "REVIEW_REQUIRED"
                overall["ok"] = False
            elif node_report["buckets"].get(READINESS_COLLECTING_BASELINE, 0) > 0:
                node_report["state"] = "COLLECTING_BASELINE"
            else:
                node_report["state"] = "MONITORING"
        overall["nodes"][node.node_id] = node_report

    print(json.dumps(overall, indent=2))
    if not auto_renewal_enabled:
        print("\nNOTE: auto_renewal_enabled is false -- no VPS above can be auto-renewed "
              "regardless of its readiness bucket. This is the normal, safe default.")
    elif not write_approved:
        print("\nNOTE: auto_renewal_enabled is true but WRITE_APPROVED does not exist -- "
              "the writer is still structurally blocked. Run 'enable-auto-renewal' to "
              "grant both together, or this is an inconsistent state worth reviewing.")
    return 0 if overall["ok"] else 1


def cmd_doctor(args):
    """Comprehensive, read-only self-diagnostic combining service health
    (install-health-check), financial-engine readiness, legacy-automation conflicts,
    credential presence, and backup freshness into ONE human-readable report -- the single
    command an administrator should need to run before asking 'is something wrong', rather
    than inspecting raw Python tracebacks, journalctl output, or multiple separate JSON
    blobs by hand."""
    import installer as inst
    problems = []
    notices = []

    health = inst.health_check(CONFIG_DIR, STATE_DIR, check_systemd=True)
    print("== Service health (install-health-check) ==")
    print(f"  {'OK' if health['ok'] else 'PROBLEMS FOUND'}")
    for p in health["problems"]:
        print(f"  ! {p}")
        problems.append(p)

    legacy = health.get("legacy_bandwidth_automation", inst.detect_legacy_bandwidth_automation())
    print("\n== Legacy bandwidth automation ==")
    if legacy["found_active"] or legacy["found_disabled"]:
        print(f"  Active:   {legacy['found_active']}")
        print(f"  Disabled: {legacy['found_disabled']}")
        if legacy["active_write_capable"]:
            notices.append("an active, write-capable foreign bandwidth job is present -- "
                            "enable-auto-renewal will refuse without an explicit override")
    else:
        print("  None detected.")

    print("\n== Credentials ==")
    try:
        nodes = nodes_mod.load_nodes(_node_config_path())
    except Exception as e:
        nodes = []
        problems.append(f"nodes.json did not load: {e}")
    for node in nodes:
        stored = os.path.exists(creds_mod.default_credentials_path(CONFIG_DIR, node.node_id))
        print(f"  {node.node_id}: {'stored' if stored else 'NOT stored (store-credentials to enable unattended apply)'}")

    print("\n== Backups ==")
    try:
        backups = backup_mod.list_backups(BACKUP_DIR)
    except Exception:
        backups = []
    if backups:
        print(f"  {len(backups)} backup(s) found, most recent: {sorted(backups)[-1]}")
    else:
        notices.append("no backups exist yet -- run 'backup' before enabling automatic renewal")
        print("  None found.")

    print("\n== Writer state ==")
    write_approved = os.path.exists(WRITE_APPROVAL_MARKER)
    print(f"  WRITE_APPROVED: {'present' if write_approved else 'absent (structurally blocked)'}")

    print("\n== Summary ==")
    if problems:
        print(f"  {len(problems)} problem(s) require attention (see above).")
    if notices:
        print(f"  {len(notices)} notice(s):")
        for n in notices:
            print(f"    - {n}")
    if not problems and not notices:
        print("  Nothing to report.")
    return 0 if not problems else 1


def cmd_logs(args):
    """Read-only: recent renewal operations (from the durable operations table -- real
    attempts, never a guess) and the most recent backups, per node. Never prints credential
    values; `api_response` is already redaction-safe at the point it was recorded (writer.py
    sanitizes before ever calling record_operation_result)."""
    limit = getattr(args, "limit", 20) or 20
    for node in nodes_mod.load_nodes(_node_config_path()):
        db_path = node.db_path(STATE_DIR)
        print(f"=== {node.node_id} ===")
        if not os.path.exists(db_path):
            print("  No database yet.")
            continue
        conn = sm.open_db(db_path)
        rows = conn.execute(
            "SELECT created_at, vpsid, status, requested_new_quota_gb, verified_after_quota_gb "
            "FROM operations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        if not rows:
            print("  No renewal operations recorded yet.")
        for created_at, vpsid, status, requested, verified in rows:
            print(f"  {created_at}  vpsid={vpsid}  {status}  requested={requested}  verified={verified}")
    try:
        backups = backup_mod.list_backups(BACKUP_DIR)
    except Exception:
        backups = []
    print(f"\n=== Backups ({len(backups)}) ===")
    for b in sorted(backups)[-limit:]:
        print(f"  {b}")
    return 0


# ---------- automatic systemd activation of the READ-ONLY core timers (v1.10.3) ----------------

def cmd_install_activate_core_timers(args):
    """Installs and enables the four READ-ONLY core timers (monitor, inventory-sync,
    hostnet-sync, readiness-notifier) into the real systemd -- copies the unit files
    `install` already generated under SYSTEMD_OUT_DIR into /etc/systemd/system, runs
    daemon-reload, then `enable --now` on each timer. Never touches the write-capable
    `apply` timer (see cmd_enable_auto_renewal for that, which is deliberately separate and
    requires its own explicit confirmation). This is the exact mechanical step that `ir2`'s
    installation required by hand, one unit at a time, with some missed entirely -- as of
    v1.10.3 `install.sh` calls this automatically when run as root (see TIXO_RENEWAL_NO_AUTO_SYSTEMD
    to opt out)."""
    if os.geteuid() != 0:
        print("ERROR: activating real systemd units requires root (to write to "
              "/etc/systemd/system and call systemctl). Re-run with sudo.")
        return 1
    import service_activation as sa
    result = sa.install_and_enable_units(SYSTEMD_OUT_DIR, sa.CORE_READONLY_UNITS)
    if result["enabled"]:
        print(f"Enabled and started: {', '.join(result['enabled'])}")
    if result["problems"]:
        print("Problems:")
        for p in result["problems"]:
            print(f"  - {p}")
    print(f"\nCore timer activation {'succeeded' if result['ok'] else 'FAILED'}.")
    if result["ok"]:
        print("These four timers are read-only by construction -- none of them can write "
              "to Virtualizor. Run 'renewal_cli.py install-health-check' any time to verify "
              "they stay enabled and none has gone into a failed state.")
    return 0 if result["ok"] else 1


# ---------- unified automatic-renewal / production-write authorization (v1.10.3) ---------------

def cmd_enable_auto_renewal(args):
    """Grants production-write authorization AND flips policy.json's auto_renewal_enabled
    AND (if root) installs/enables the write-capable apply timer -- all three together, in
    one confirmed action, via installer.enable_auto_renewal(). Requires --yes (scripted) or
    interactive confirmation; this is the single most consequential switch in the whole
    product and must never flip silently. Also refuses (see installer.enable_auto_renewal's
    docstring) if a foreign, write-capable bandwidth automation job is detected already
    active on this host, unless --override-legacy-write-conflict is explicitly passed."""
    if not getattr(args, "yes", False):
        print("This will:")
        print("  1. Create renewal-config/WRITE_APPROVED (production write capability).")
        print("  2. Set policy.json global_defaults.auto_renewal_enabled = true.")
        print("  3. Install and enable the write-capable virt-renewal-apply.timer (requires root).")
        print("\nAfter this, the apply timer will actually write bandwidth renewals to "
              "Virtualizor for any VPS its policy marks eligible. Re-run with --yes to confirm.")
        return 1
    import installer as inst
    result = inst.enable_auto_renewal(
        CONFIG_DIR, SYSTEMD_OUT_DIR,
        override_legacy_write_conflict=getattr(args, "override_legacy_write_conflict", False))
    for step in result["steps"]:
        print(f"  - {step}")
    for problem in result["problems"]:
        print(f"  ! {problem}")
    print(f"\nAutomatic renewal {'ENABLED' if result['ok'] else 'enable FAILED'}.")
    return 0 if result["ok"] else 1


def cmd_disable_auto_renewal(args):
    """Revokes production-write authorization: removes WRITE_APPROVED, sets
    auto_renewal_enabled=false, and (if root) stops/disables/removes the apply timer --
    always together, never a partial state where auto_renewal_enabled=false but
    WRITE_APPROVED still exists. Monitoring/inventory/hostnet/notifier timers and all
    historical ledger data are explicitly untouched."""
    import installer as inst
    result = inst.disable_auto_renewal(CONFIG_DIR, SYSTEMD_OUT_DIR)
    for step in result["steps"]:
        print(f"  - {step}")
    for problem in result["problems"]:
        print(f"  ! {problem}")
    print(f"\nAutomatic renewal {'DISABLED' if result['ok'] else 'disable reported problems'}.")
    return 0 if result["ok"] else 1


# ---------- update (real download/verify/apply, v1.10.2) ----------------------------------------

def cmd_update_check(args):
    """Read-only: current vs. latest published GitHub release, with release notes. Never
    downloads or applies anything -- see `update-apply` for that, which always requires
    either an interactive confirmation or an explicit --yes."""
    import updater
    result = updater.fetch_release_info()
    if not result["ok"]:
        print(f"Could not check for updates: {result['error']}")
        return 1
    newer = menu_is_newer_version(result["version"], VERSION)
    print(f"Installed version: {VERSION}")
    print(f"Latest release:    {result['version']} ({result['tag']})")
    if newer:
        print("\nAn update is available.")
        if result["notes"]:
            print("\nRelease notes:\n" + result["notes"])
        print(f"\nTo apply: tixo-renewal update --yes   (or use the menu's "
              f"'Check for updates' screen for an interactive confirmation)")
    else:
        print("\nAlready up to date.")
    return 0


def _lazy_menu():
    """menu.py imports renewal_cli at module level (for VERSION/paths), so renewal_cli
    cannot import menu.py at ITS module level without a circular import -- deferred here,
    used only by the two update commands that want menu.py's already-tested semantic
    version comparison instead of re-implementing it."""
    import menu as menu_mod
    return menu_mod


def menu_is_newer_version(latest, current) -> bool:
    return _lazy_menu().is_newer_version(latest, current)


def cmd_update_apply(args):
    """Downloads, verifies, and applies a release (default: latest) via updater.apply_update
    -- full safety backup first, atomic-ish code swap (old code moved aside, never deleted,
    until a post-update health check passes), automatic rollback on any failure. Requires
    either an interactive 'yes' confirmation or --yes (for scripted/CI use, e.g. a tested
    upgrade pipeline) -- NEVER applies without one or the other. Restarts only the
    application's own Web/Telegram services if they were active before the update (never
    any other service, never Virtualizor, never a host reboot). Never touches renewal
    policy, VPS quotas, or write-approval state."""
    import updater
    target_tag = f"v{args.version}" if args.version else None
    info = updater.fetch_release_info(tag=target_tag)
    if not info["ok"]:
        print(f"Could not fetch release info: {info['error']}")
        return 1

    menu_mod = _lazy_menu()
    if not menu_mod.is_newer_version(info["version"], VERSION) and not args.force:
        print(f"Installed version ({VERSION}) is already >= latest release "
              f"({info['version']}) -- nothing to do. Pass --force to re-apply anyway.")
        return 0

    print(f"Current version: {VERSION}")
    print(f"Target version:  {info['version']} ({info['tag']})")
    if info["notes"]:
        print(f"\nRelease notes:\n{info['notes']}\n")

    if not args.yes:
        confirm = input(f"Apply update to {info['tag']}? This will take a full backup first "
                         f"and roll back automatically on any failure. Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Cancelled.")
            return 1

    # Determine which application-owned services were active BEFORE the update, so we
    # restart only those, and only after the new code has passed its health check.
    services_to_restart = []
    for svc in ("virt-renewal-web.service", "virt-renewal-telegram-bot.service"):
        try:
            result = subprocess.run(["systemctl", "is-active", svc], capture_output=True,
                                     text=True, timeout=5)
            if result.stdout.strip() == "active":
                services_to_restart.append(svc)
        except (OSError, subprocess.SubprocessError):
            pass  # systemctl unavailable (e.g. a non-systemd dev/test environment) -- skip

    print("\nDownloading, verifying, and applying update...")
    result = updater.apply_update(WORKSPACE_ROOT, CONFIG_DIR, STATE_DIR, BACKUP_DIR,
                                   tag=info["tag"])

    if not result["ok"]:
        print(f"\nUPDATE FAILED at stage '{result['stage']}': {result['error']}")
        if result["rolled_back"]:
            print("Automatically rolled back -- the previous working install is unchanged.")
        if result["backup_path"]:
            print(f"Full safety backup (unaffected either way): {result['backup_path']}")
        return 1

    print(f"\nUpdate applied successfully: v{VERSION} -> {info['tag']}")
    print(f"Full pre-update safety backup: {result['backup_path']}")

    for svc in services_to_restart:
        print(f"Restarting {svc} (was active before the update)...")
        try:
            subprocess.run(["systemctl", "restart", svc], timeout=15, check=True)
            print(f"  {svc}: restarted.")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  {svc}: WARNING -- could not restart automatically ({e}). "
                  f"Restart it manually: sudo systemctl restart {svc}")

    print("\nRe-run 'tixo-renewal doctor' (or 'tixo-renewal health-check', or reopen the "
          "menu) to verify.")
    return 0


# ---------- uninstall ----------

UNINSTALL_UNIT_NAMES = (
    "virt-renewal-monitor.service", "virt-renewal-monitor.timer",
    "virt-renewal-apply.service", "virt-renewal-apply.timer",
    "virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer",
    "virt-renewal-inventory-sync.service", "virt-renewal-inventory-sync.timer",
    "virt-renewal-hostnet-sync.service", "virt-renewal-hostnet-sync.timer",
    "virt-renewal-telegram-bot.service",
    "virt-renewal-web.service", "virt-renewal-relay-worker.service", "virt-renewal-agent.service",
    "virt-renewal-backup.service", "virt-renewal-backup.timer",
)


REQUIRED_PURGE_PHRASE = "PURGE ALL TIXOCLOUD DATA"


def cmd_uninstall_preview(args):
    """Read-only. Shows exactly what `uninstall`/`uninstall --purge` WOULD do -- never
    modifies anything. Always run this (or let the menu show it) before confirming either
    destructive mode."""
    import uninstaller as un
    import json as _json
    preview = un.preview_uninstall(WORKSPACE_ROOT, purge=args.purge)
    print(_json.dumps(preview, indent=2))
    return 0


def cmd_uninstall(args):
    """Real two-mode uninstall (v1.10.2), replacing the previous version which only ever
    removed LOCALLY GENERATED systemd unit template copies and printed manual commands for
    the operator to run themselves. See uninstaller.py's module docstring for the full
    ownership-verification/safety discipline this builds on.

    Standard mode (default): stops+disables+removes every actually-installed owned systemd
    unit, removes the global 'tixo-renewal' wrapper, removes application CODE (lib/, bin/,
    local systemd template copies) -- PRESERVES renewal-config/, shadow-monitor-state/, and
    backups/ for possible recovery.

    --purge (full purge): everything above PLUS removes renewal-config/ and
    shadow-monitor-state/ (config, secrets, databases, ledgers). Requires
    --confirm="PURGE ALL TIXOCLOUD DATA" (exact match) when --yes is also passed (for
    scripted/tested use); otherwise prompts interactively for that exact phrase. Backups
    are NEVER removed by either mode -- see the separate `uninstall --purge --delete-backups`
    flag, which still requires its own confirmation.

    Never touches Virtualizor, customer VPS data, or any file/service this project's own
    installer did not create."""
    import uninstaller as un
    import json as _json

    if args.purge:
        if args.yes:
            if args.confirm != REQUIRED_PURGE_PHRASE:
                print(f"--purge with --yes requires --confirm={REQUIRED_PURGE_PHRASE!r} "
                      f"(exact match) -- refusing to proceed without it.")
                return 1
        else:
            print(f"This will PERMANENTLY remove all TixoCloud application code, "
                  f"configuration, secrets, databases, and renewal ledgers under "
                  f"{WORKSPACE_ROOT}.")
            print(f"Backups under {WORKSPACE_ROOT}/backups are NOT removed by this.")
            typed = input(f"Type exactly '{REQUIRED_PURGE_PHRASE}' to proceed, anything "
                           f"else cancels: ")
            if typed != REQUIRED_PURGE_PHRASE:
                print("Cancelled.")
                return 1
        report = un.full_purge(WORKSPACE_ROOT)
    else:
        if not args.yes:
            print(f"This will stop/disable/remove TixoCloud's systemd units and remove its "
                  f"application code under {WORKSPACE_ROOT}. Configuration, secrets, "
                  f"databases, and backups are PRESERVED.")
            if input("Type 'UNINSTALL' to proceed, anything else cancels: ") != "UNINSTALL":
                print("Cancelled.")
                return 1
        report = un.standard_uninstall(WORKSPACE_ROOT)

    print(_json.dumps(report, indent=2))

    if args.delete_backups:
        if not args.yes:
            backup_dir = os.path.join(WORKSPACE_ROOT, "backups")
            existing = os.listdir(backup_dir) if os.path.isdir(backup_dir) else []
            print(f"\nThe following {len(existing)} backup archive(s) would be permanently "
                  f"deleted:")
            for name in existing:
                print(f"  {name}")
            if input("Type 'DELETE BACKUPS' to proceed, anything else cancels: ") != "DELETE BACKUPS":
                print("Backups kept.")
                return 0 if not report["problems"] else 1
        backup_report = un.delete_backups(WORKSPACE_ROOT)
        print(_json.dumps(backup_report, indent=2))

    verify = un.verify_no_residue(WORKSPACE_ROOT, purge=args.purge)
    if not verify["ok"]:
        print("\nWARNING: residual TixoCloud-owned artifacts remain after uninstall:")
        for p in verify["problems"]:
            print(f"  {p}")
        return 1

    if report["problems"] or report["unknown_entries"]:
        print("\nUninstall completed with issues -- see 'problems'/'unknown_entries' above. "
              "This was NOT a silent/complete purge.")
        return 1

    print("\nUninstall complete. No TixoCloud-owned processes, timers, services, or "
          "executable launchers remain.")
    return 0


# ---------- apply (structurally blocked without explicit approval) ----------

def cmd_apply(args):
    """A real writer (writer.apply_renewal, via Virtualizor's managevps API, never direct
    SQL) exists and is tested -- but this command remains structurally impossible to trigger
    automatically. It requires ALL of: (1) the operator-created WRITE_APPROVED marker,
    (2) an explicit --execute flag on this exact invocation, (3) interactively-typed live
    API credentials (never stored, never read from disk/env). No systemd unit ever calls
    `apply` -- only `dry-run`. This satisfies 'a writer exists' without satisfying 'it can run
    unattended,' which is the actual safety property required right now."""
    if not os.path.exists(WRITE_APPROVAL_MARKER):
        print("BLOCKED: production write capability is not approved.")
        print(f"To enable it, the OPERATOR must explicitly create: {WRITE_APPROVAL_MARKER}")
        print("This agent will not create that file on its own. See DEPLOYMENT_PLAN_V2.md.")
        return 1
    if not args.execute:
        print("Approval marker present, but this was a preview call (no --execute given).")
        print("Re-run with --node, --vpsid, --period-key, --new-quota-gb, and --execute to")
        print("actually attempt a write -- you will be prompted for live API credentials.")
        return 1
    if not (args.node and args.vpsid is not None and args.period_key and args.new_quota_gb):
        print("--execute requires --node, --vpsid, --period-key, and --new-quota-gb.")
        return 1

    import writer as writer_mod
    import local_source as ls

    _requested_decimal = Decimal(args.new_quota_gb)
    _normalized_decimal = pol.round_quota_for_virtualizor(_requested_decimal)
    print("You are about to WRITE to production Virtualizor via its admin API.")
    print(f"  node={args.node} vpsid={args.vpsid} period_key={args.period_key} "
          f"requested_new_quota_gb={args.new_quota_gb}")
    if _normalized_decimal != _requested_decimal:
        print(f"  NOTE: Virtualizor's bandwidth field is integer-only -- the actual value "
              f"sent will be normalized to {_normalized_decimal} GB (rounded up from "
              f"{_requested_decimal} GB), not the exact fractional value typed above.")

    node = next((n for n in nodes_mod.load_nodes(_node_config_path()) if n.node_id == args.node), None)
    if node is None:
        print(f"Unknown node: {args.node}")
        return 1

    if getattr(args, "non_interactive", False):
        # Unattended path: credentials come ONLY from the operator-populated secure store
        # (see `store-credentials`), never from env/disk elsewhere, never typed here. No
        # interactive 'APPLY' confirmation is possible unattended -- the WRITE_APPROVED
        # marker + this exact command's flags are the only gates in that mode.
        try:
            stored = creds_mod.load_credentials(creds_mod.default_credentials_path(CONFIG_DIR, args.node))
        except (FileNotFoundError, creds_mod.CredentialSecurityError) as e:
            print(f"BLOCKED: cannot load stored credentials for non-interactive apply: {e}")
            return 1
        creds = writer_mod.ApiCredentials(base_url=stored.base_url, api_key=stored.api_key, api_pass=stored.api_pass)
    else:
        confirm_text = input("Type EXACTLY 'APPLY' to proceed, anything else cancels: ")
        if confirm_text != "APPLY":
            print("Cancelled.")
            return 1
        import getpass
        base_url = input("Virtualizor admin API base URL (e.g. https://host:4085/index.php): ").strip()
        api_key = getpass.getpass("Admin API key: ")
        api_pass = getpass.getpass("Admin API password: ")
        creds = writer_mod.ApiCredentials(base_url=base_url, api_key=api_key, api_pass=api_pass)

    conn = sm.open_db(node.db_path(STATE_DIR))
    result = writer_mod.apply_renewal(
        conn, args.node, int(args.vpsid), args.period_key, args.new_quota_gb, creds,
        WRITE_APPROVAL_MARKER, confirm=True,
        read_live_quota_fn=lambda vpsid: ls.fetch_single_vps_quota(vpsid),
    )
    conn.close()
    print(f"Result: {result}")
    if result["status"] != "APPLIED":
        print("Not confirmed as applied -- see `resolve-operation` to investigate before any retry.")
        return 1
    return 0


# ---------- store-credentials (operator-run, interactive, ONE TIME) ----------------------------

def cmd_store_credentials(args):
    """The ONLY command that ever writes to the secure credential store, and it always
    prompts interactively -- there is no flag to feed it a password non-interactively, so a
    credential can never be planted by a script or a compromised automation step, only by a
    human sitting at this exact terminal."""
    import getpass
    path = creds_mod.default_credentials_path(CONFIG_DIR, args.node)
    print(f"Storing credentials for node '{args.node}' at {path} (mode 0600).")
    base_url = input("Virtualizor admin API base URL (e.g. https://host:4085/index.php): ").strip()
    api_key = getpass.getpass("Admin API key: ")
    api_pass = getpass.getpass("Admin API password: ")
    creds_mod.store_credentials(path, base_url, api_key, api_pass)
    print("Stored. This does NOT approve writes by itself -- WRITE_APPROVED must still exist "
          "separately, and nothing is applied until `apply` or `apply-due` is run.")


def cmd_validate_credentials(args):
    """Makes ONE real network call to Virtualizor's strictly read-only `listvs` action (see
    writer.probe_api_reachability / writer.call_listvs_api) -- no vpsid, no edit parameters,
    no POST body, so no real VPS can be affected. Still requires explicit invocation; never
    run by any systemd unit or automatically by any other command.

    SECURITY: any failure here (malformed URL, network error, etc.) is guaranteed by
    writer.py to reach this function as either InvalidBaseUrlError or SanitizedNetworkError,
    both of which have already had api_key/api_pass stripped from their message. This
    function's own try/except below is a second, independent layer in case of a future
    mistake upstream -- it never prints `stored`/`creds` directly, only their already-public
    base_url and the pre-sanitized exception text."""
    import writer as writer_mod
    path = creds_mod.default_credentials_path(CONFIG_DIR, args.node)
    try:
        stored = creds_mod.load_credentials(path)
    except (FileNotFoundError, creds_mod.CredentialSecurityError) as e:
        print(f"Cannot load stored credentials: {e}")
        print(f"Run `store-credentials --node {args.node}` first.")
        return 1
    creds = writer_mod.ApiCredentials(base_url=stored.base_url, api_key=stored.api_key, api_pass=stored.api_pass)
    print(f"Probing {stored.base_url} via the read-only `listvs` action -- no vpsid, no edit "
          f"parameters, no POST body; this request cannot write anything under any response.")
    try:
        result = writer_mod.probe_api_reachability(creds)
    except Exception as e:  # second, independent safety net -- see docstring above
        import writer as _w
        print(f"Probe failed: {_w._redact_secret_text(str(e), creds.api_key, creds.api_pass)}")
        return 1
    print(f"Confidence: {result['confidence']}")
    print(f"Detail: {result['detail']}")
    if result["confidence"] != "CREDENTIALS_LIKELY_VALID":
        print("This is a hint for you to interpret, not an automated pass/fail -- see "
              "writer.probe_api_reachability()'s docstring for its exact (unverified) logic.")
        print(f"Run `diagnose-api --node {args.node}` for a full sanitized structural report.")
        return 1
    return 0


def cmd_diagnose_api(args):
    """STRICTLY READ-ONLY diagnostic: one GET to the same `act=listvs` endpoint (no vpsid,
    no edit parameters, no POST body) and a SANITIZED structural report -- HTTP status,
    response Content-Type, top-level JSON key NAMES, and type/count-only shape. Never prints
    the raw response body, any field value, customer data, credentials, or the credential-
    bearing request URL. Use this when `validate-credentials` reports UNKNOWN to see what the
    real response actually looks like, then refine writer.probe_api_reachability()
    accordingly."""
    import writer as writer_mod
    path = creds_mod.default_credentials_path(CONFIG_DIR, args.node)
    try:
        stored = creds_mod.load_credentials(path)
    except (FileNotFoundError, creds_mod.CredentialSecurityError) as e:
        print(f"Cannot load stored credentials: {e}")
        return 1
    creds = writer_mod.ApiCredentials(base_url=stored.base_url, api_key=stored.api_key, api_pass=stored.api_pass)
    print("Diagnosing the read-only `listvs` response shape (no vpsid, no mutation "
          "parameters). Only HTTP status, content-type, key names, and types are reported "
          "below -- never raw values.")
    try:
        diag = writer_mod.diagnose_listvs_response(creds)
    except Exception as e:
        print(f"Diagnostic failed: {writer_mod._redact_secret_text(str(e), creds.api_key, creds.api_pass)}")
        return 1
    if diag.get("error"):
        print(f"Error: {diag['error']}")
        return 1
    print(f"HTTP status: {diag['status']}")
    print(f"Content-Type: {diag['content_type']}")
    print(f"Top-level JSON keys: {diag['top_level_keys']}")
    print(f"Sanitized structure: {json.dumps(diag['structure'], indent=2)}")
    return 0


def cmd_inspect_vps(args):
    """STRICTLY READ-ONLY: fetches a single VPS's identity, quota, and used-bandwidth directly
    from the local Virtualizor database. No API calls, no writes, no credential handling."""
    node = next((n for n in nodes_mod.load_nodes(_node_config_path()) if n.node_id == args.node), None)
    if node is None:
        print(f"Unknown node: {args.node}")
        return 1
    import local_source as ls
    try:
        row = ls.fetch_single_vps_all(args.vpsid)
    except ls.LocalSourceError as e:
        print(f"Not found: {e}")
        return 1
    print(f"=== VPS {row['vpsid']} [READ-ONLY INSPECTION] ===")
    print(f"  Source:           local Virtualizor DB (universal.php, single SELECT)")
    print(f"  vpsid:            {row['vpsid']}")
    print(f"  created_epoch:    {row['created_epoch']}")
    print(f"  quota_gb:         {row['quota_gb']} GB")
    print(f"  used_gb:          {row['used_gb']} GB")
    return 0


# ---------- apply-due (the unattended-renewal entry point; still fully gated) -------------------

def cmd_apply_due(args):
    """Scans one node's most recent dry-run data for VPS whose resolved policy has
    auto_renewal_enabled=True AND whose last detection was a plausible, eligible renewal
    candidate, and applies each through the SAME writer safety machinery as `apply` (durable
    operations table, pre/post live verification, never-retry-blind on UNKNOWN). This is what
    the (unenabled, unless enable-auto-renewal has run) unattended systemd timer would run.
    Still requires WRITE_APPROVED; with --non-interactive it also requires credentials
    already stored via `store-credentials`.

    Exit code 3 (not 1) for the 'not approved' case specifically: this command is the
    ExecStart of the write-capable virt-renewal-apply.service, whose unit file declares
    `SuccessExitStatus=3` for exactly this reason -- a deliberate, by-design safety refusal
    must be visually distinguishable in `systemctl status`/journalctl from an actual crash
    (exit 1, uncaught traceback, etc.); conflating the two was a real, reported source of
    operator confusion (misread as 'the service is broken' when it was correctly refusing to
    run unattended without authorization)."""
    if not os.path.exists(WRITE_APPROVAL_MARKER):
        print("BLOCKED (by design, not an error): production write capability is not approved.")
        print(f"To enable it, use the menu's 'Enable automatic renewal' screen, or run: "
              f"renewal_cli.py enable-auto-renewal --yes")
        return 3

    node = next((n for n in nodes_mod.load_nodes(_node_config_path()) if n.node_id == args.node), None)
    if node is None:
        print(f"Unknown node: {args.node}")
        return 1

    import writer as writer_mod
    import local_source as ls
    from decimal import Decimal
    from engine import renewal_period_index

    if getattr(args, "non_interactive", False):
        try:
            stored = creds_mod.load_credentials(creds_mod.default_credentials_path(CONFIG_DIR, args.node))
        except (FileNotFoundError, creds_mod.CredentialSecurityError) as e:
            print(f"BLOCKED: {e}")
            return 1
        creds = writer_mod.ApiCredentials(base_url=stored.base_url, api_key=stored.api_key, api_pass=stored.api_pass)
    else:
        import getpass
        base_url = input("Virtualizor admin API base URL: ").strip()
        creds = writer_mod.ApiCredentials(base_url=base_url, api_key=getpass.getpass("Admin API key: "),
                                           api_pass=getpass.getpass("Admin API password: "))

    cfg = pol.load_policy_config(_policy_config_path())
    conn = sm.open_db(node.db_path(STATE_DIR))
    checkpoint = conn.execute("SELECT first_poll_at FROM monitor_checkpoints WHERE id=1").fetchone()
    vpsids = [r[0] for r in conn.execute("SELECT DISTINCT vpsid FROM identities")]

    applied, skipped, deferred = [], [], []
    for vpsid in vpsids:
        # CRITICAL: one VPS's exception (network blip, unexpected API shape, etc.) must never
        # abort processing of every OTHER eligible VPS in this run. Each vpsid is fully
        # isolated; a failure here is recorded as skipped, not a crash of the whole batch.
        try:
            # Check for a pending deferred (backup/restore/migration-locked) retry FIRST,
            # before computing any fresh baseline -- a locked VPS may not show a fresh reset
            # pattern in the CURRENT period while still locked, but a previously-deferred
            # attempt for an EARLIER period must still be retried using its ORIGINALLY
            # computed quota, never recomputed from whatever USED looks like right now.
            deferred_result = writer_mod.retry_deferred_renewal(
                conn, args.node, vpsid, creds, WRITE_APPROVAL_MARKER,
                read_live_quota_fn=lambda v: ls.fetch_single_vps_quota(v))
            if deferred_result["status"] != "NO_DEFERRED_OPERATION":
                deferred.append((vpsid, deferred_result))
                time.sleep(API_CALL_SPACING_SECONDS)  # this retry touched the real API
                continue

            ident = conn.execute("SELECT created_epoch FROM identities WHERE vpsid=?", (vpsid,)).fetchone()
            # apply-due must NEVER act on a hypothetical "as if renewed right now" baseline --
            # only a REAL observed reset pattern (two snapshots: quota unchanged, usage dropped)
            # is eligible for actual application. See _renewal_baseline_for_vpsid()'s docstring
            # for the exact bug this guards against (using post-drop usage as the pre-drop value).
            baseline = _renewal_baseline_for_vpsid(conn, vpsid)
            if baseline is None:
                skipped.append((vpsid, "NO_OBSERVED_RESET_PATTERN"))
                continue
            old_quota, old_used, event_ts = baseline
            preview = pi.preview_renewal(
                cfg, args.node, vpsid, verified_created_epoch=ident[0] if ident else None,
                node_first_poll_at=checkpoint[0] if checkpoint else None,
                old_quota=old_quota, old_used=old_used, event_ts=event_ts,
            )
            if preview["status"] != "PLAUSIBLE_RENEWAL_CANDIDATE":
                skipped.append((vpsid, preview["status"]))
                continue
            period_index = renewal_period_index(preview["anchor_epoch"], event_ts)
            period_key = f"{vpsid}|{preview['anchor_epoch']}|{period_index}"
            result = writer_mod.apply_renewal(
                conn, args.node, vpsid, period_key, Decimal(preview["proposed_new_quota_gb"]), creds,
                WRITE_APPROVAL_MARKER, confirm=True,
                read_live_quota_fn=lambda v: ls.fetch_single_vps_quota(v),
            )
            if result["status"] == "DEFERRED_LOCKED":
                deferred.append((vpsid, result))
            else:
                applied.append((vpsid, result))
            time.sleep(API_CALL_SPACING_SECONDS)  # this attempt touched the real API
        except Exception as e:
            skipped.append((vpsid, f"ERROR: {e}"))
            continue

    conn.close()
    print(f"Applied/attempted: {len(applied)}, deferred (locked): {len(deferred)}, "
          f"skipped (not due/approved/errored): {len(skipped)}")
    for vpsid, result in deferred:
        print(f"  DEFERRED vpsid={vpsid}: {result}")
    for vpsid, result in applied:
        print(f"  vpsid={vpsid}: {result}")
    return 0


# ---------- resolve-operation (manual, read-only investigation of an UNKNOWN operation) --------

def cmd_resolve_operation(args):
    """Lists unresolved (IN_PROGRESS / UNKNOWN_NEEDS_VERIFICATION) operations for a node, and
    optionally lets the operator mark one resolved after they've manually verified the true
    live state themselves. Never re-attempts a write -- that always requires a fresh `apply`."""
    node = next((n for n in nodes_mod.load_nodes(_node_config_path()) if n.node_id == args.node), None)
    if node is None:
        print(f"Unknown node: {args.node}")
        return 1
    conn = sm.open_db(node.db_path(STATE_DIR))
    rows = conn.execute(
        "SELECT operation_id, vpsid, period_key, requested_new_quota_gb, status, "
        "created_at, api_response FROM operations "
        "WHERE status IN ('IN_PROGRESS','UNKNOWN_NEEDS_VERIFICATION') ORDER BY created_at"
    ).fetchall()
    if not rows:
        print(f"No unresolved operations for node {args.node}.")
        conn.close()
        return 0
    for r in rows:
        print(f"  operation_id={r[0]} vpsid={r[1]} period={r[2]} requested={r[3]} "
              f"status={r[4]} created_at={r[5]} api_response={r[6]}")
    if args.mark_resolved:
        if args.resolution not in ("APPLIED", "FAILED"):
            print("--resolution must be APPLIED or FAILED -- the operator must state which, "
                  "based on their OWN live verification; this tool will not guess.")
            conn.close()
            return 1
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE operations SET status=?, updated_at=?, verified_after_quota_gb=? "
            "WHERE operation_id=?",
            (args.resolution, sm.utcnow_iso(), args.verified_quota_gb, args.mark_resolved),
        )
        conn.execute("COMMIT")
        print(f"Marked {args.mark_resolved} as {args.resolution} (operator-confirmed via live "
              f"verification). A future `apply` for a NEW period may now proceed; the SAME "
              f"period is still permanently recorded either way.")
    conn.close()


# ---------- CLI wiring ----------

def cmd_hostinfo(args):
    """Read-only host hardware/OS report, noninteractive. Never touches Virtualizor or any
    VPS -- see implementation/hostinfo.py for what "unavailable" vs "unsupported" vs
    "permission_denied" means for each field."""
    import hostinfo as hostinfo_mod
    info = hostinfo_mod.collect_host_info()
    import json
    print(json.dumps(info, indent=2, default=str))
    return 0


def cmd_virtualizor_info(args):
    """Read-only Virtualizor installation/version/API-connectivity report, noninteractive."""
    import virtualizor_info as vinfo_mod
    report = {
        "installed": vinfo_mod.detect_virtualizor_installed(),
        "version": vinfo_mod.detect_virtualizor_version(),
        "virtualization_technologies": vinfo_mod.detect_virtualization_technologies(),
    }
    if getattr(args, "node", None):
        path = creds_mod.default_credentials_path(CONFIG_DIR, args.node)
        if os.path.exists(path):
            stored = creds_mod.load_credentials(path)
            report["api"] = vinfo_mod.probe_api_latency(stored)
    import json
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_inventory_sync(args):
    """One read-only, rate-limited, non-overlapping VPS inventory refresh. Never writes to
    Virtualizor, never touches WRITE_APPROVED, never shares a lock/retry-budget with the
    renewal engine's own apply path.

    Exit 0 (not 1) when credentials simply haven't been stored YET: this command is the
    ExecStart of the auto-activated (v1.10.3+) virt-renewal-inventory-sync.timer, which runs
    on every fresh install before an operator has necessarily had the chance to run
    `store-credentials` -- treating "not configured yet" as a timer/service FAILURE (as this
    used to) is exactly the "deliberate safety block looks like a crash" problem this
    project's own design principles explicitly call out (see cmd_apply_due's matching
    SuccessExitStatus=3 fix for the write-capable timer); the read-only timers get the
    simpler fix of just not treating this as an error at all, since there is nothing unsafe
    about it."""
    import inventory as inv_mod
    import inventory_sync as isync_mod
    path = creds_mod.default_credentials_path(CONFIG_DIR, args.node)
    try:
        stored = creds_mod.load_credentials(path)
    except (FileNotFoundError, creds_mod.CredentialSecurityError) as e:
        print(f"status=NOT_CONFIGURED (credentials not yet stored: {e})")
        return 0
    conn = inv_mod.open_db(os.path.join(STATE_DIR, "inventory.sqlite3"))
    result = isync_mod.run_inventory_sync(conn, args.node, stored, STATE_DIR)
    print(f"status={result['status']}")
    if result["status"] == "OK":
        d = result["diff"]
        print(f"added={d['added_count']} removed={d['removed_count']} modified={d['modified_count']}")
        if d["quota_changed"]:
            print(f"quota_changed_vpsid={d['quota_changed']}")
        return 0
    if result.get("error"):
        import writer as writer_mod
        print(f"detail={writer_mod.redact_url_patterns(str(result['error']))}")
    return 0 if result["status"] in ("OK", "ALREADY_RUNNING") else 1


def cmd_inventory_status(args):
    """Read-only summary of the inventory snapshot database -- last sync time, record count,
    freshness classification. Never touches the renewal ledger."""
    import inventory as inv_mod
    import inventory_sync as isync_mod
    conn = inv_mod.open_db(os.path.join(STATE_DIR, "inventory.sqlite3"))
    sync_info = inv_mod.last_sync_info(conn, args.node)
    state = isync_mod.load_sync_state(STATE_DIR)
    freshness = isync_mod.classify_freshness(
        sync_info["captured_at"] if sync_info else None, state["last_attempt_status"],
        isync_mod.DEFAULT_REFRESH_INTERVAL_MIN)
    print(f"freshness={freshness}")
    print(f"last_sync={sync_info}")
    print(f"last_attempt_status={state['last_attempt_status']}")
    return 0


def cmd_hostnet_sync(args):
    """One bounded, local-only host network-traffic sample. Reads ONLY local kernel
    interface counters -- no Virtualizor API call of any kind, never touches
    WRITE_APPROVED, never shares a lock with inventory/renewal timers."""
    import hostnet_sync as hns_mod
    result = hns_mod.run_hostnet_sample(STATE_DIR, CONFIG_DIR)
    print(f"status={result['status']}")
    if result.get("error"):
        print(f"error={result['error']}")
    if result["status"] == "OK":
        s = result["sample"]
        print(f"interface={s['interface']} rx_delta={s['rx_delta']} tx_delta={s['tx_delta']} "
              f"discontinuity={s['discontinuity_type']}")
    return 0 if result["status"] in ("OK", "NOT_CONFIGURED", "ALREADY_RUNNING") else 1


def cmd_hostnet_configure(args):
    """Noninteractive configuration for headless installations. Requires --yes as the
    explicit confirmation gate (the same safety property the interactive menu enforces via
    a y/N prompt) -- never configures anything without it. --interface is required; this
    command never guesses on an operator's behalf, even when auto-detection would have
    identified a single high-confidence candidate (use `hostnet-suggest` first to see what
    would be recommended, then pass that name explicitly here)."""
    import hostnet_sync as hns_mod
    if not getattr(args, "yes", False):
        print("Refusing to configure without --yes (explicit confirmation required).")
        return 1
    try:
        hns_mod.save_monitored_interface(CONFIG_DIR, args.interface, confirmed=True,
                                          state_dir=STATE_DIR)
    except ValueError as e:
        print(f"Rejected: {e}")
        return 1
    print(f"Now monitoring '{args.interface}'.")
    return 0


def cmd_hostnet_suggest(args):
    """Read-only, noninteractive: prints the topology-aware candidate list and any auto-
    detected high-confidence selection, without saving anything -- the headless-install
    counterpart to the interactive menu's candidate display."""
    import hostnet_sync as hns_mod
    import net_topology
    result = hns_mod.suggest_interfaces()
    print(f"auto_selected={result['auto_selected']}")
    for c in result["candidates"]:
        print(f"candidate: {c['name']}  {net_topology.recommendation_reason(c)}")
    for e in result["excluded"]:
        print(f"excluded: {e['name']}  reason={e['exclusion_reason']}")
    return 0


def cmd_hostnet_status(args):
    """Read-only summary of host network accounting: configured interface, cumulative
    totals, last sample, live throughput if available."""
    import hostnet as hn_mod
    import hostnet_sync as hns_mod
    settings = hns_mod.load_settings(CONFIG_DIR)
    interface = settings.get("interface")
    print(f"interface={interface}")
    if not interface:
        print("status=NOT_CONFIGURED")
        return 0
    conn = hn_mod.open_db(os.path.join(STATE_DIR, "hostnet.sqlite3"))
    cumulative = hn_mod.get_cumulative(conn, interface)
    print(f"cumulative={cumulative}")
    live = hn_mod.compute_live_throughput(conn, interface)
    print(f"live_throughput_bytes_per_sec={live}")
    return 0


def cmd_install_ui(args):
    """Explicit, operator-initiated installation of the OPTIONAL Textual UI dependency.
    NEVER run automatically by install.sh or any other command -- the one-command installer's
    zero-third-party-dependency guarantee stays intact for everyone who doesn't run this.
    Requires either an interactive 'yes' confirmation or --yes; installs into the current
    user's site-packages via `pip install --user textual` (list-of-args subprocess, never
    shell=True). If Textual is already importable, this is a no-op."""
    try:
        import textual  # noqa: F401
        print(f"Textual is already installed (version {getattr(textual, '__version__', '?')}).")
        return 0
    except ImportError:
        pass
    print("This will install the optional 'textual' package (and its own dependencies) via:")
    print(f"  {sys.executable} -m pip install --user textual")
    print("This is NEVER done automatically -- it only runs because you explicitly asked for it.")
    if not getattr(args, "yes", False):
        reply = input("Proceed? [y/N]: ").strip().lower()
        if reply != "y":
            print("Cancelled.")
            return 1
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "textual"])
    if result.returncode != 0:
        print("pip install failed -- the lightweight CLI menu remains fully functional "
              "without Textual.")
        return 1
    print("Textual installed. The TUI will be used automatically next time you run "
          "`tixo-renewal` in an interactive terminal (use `tixo-renewal --cli` to force the "
          "lightweight menu).")
    return 0


def cmd_check_exhausted(args):
    """Noninteractive counterpart to the menu's 'Check exhausted VPS bandwidth' screen --
    same shared implementation (emergency_policy.check_fleet_exhaustion), suitable for a
    future systemd timer (NOT yet installed/enabled anywhere -- this only adds the command
    itself, per the explicit instruction not to alter production timers this session).
    Makes NO Virtualizor API call: reads only the already-synced local inventory cache."""
    import emergency_policy as emp_mod
    import inventory as inv_mod

    policy = emp_mod.load_policy_settings(CONFIG_DIR)
    if policy.mode == emp_mod.MODE_DISABLED:
        print("Exhausted-bandwidth policy is disabled -- nothing to check.")
        return 0
    conn_inv = inv_mod.open_db(os.path.join(STATE_DIR, "inventory.sqlite3"))
    records = inv_mod.latest_records(conn_inv, getattr(args, "node", None) or "self")
    if not records:
        print("No inventory records available yet -- run inventory-sync first.")
        return 1
    conn_ep = emp_mod.open_db(os.path.join(STATE_DIR, "emergency_policy.sqlite3"))
    result = emp_mod.check_fleet_exhaustion(conn_ep, records, policy)
    for item in result["actionable"]:
        print(f"VPS {item['vpsid']} ({item['hostname']}): {item['result']}")
    print(f"Checked {result['total_checked']} VPS record(s) against mode={policy.mode}.")
    for label, count in sorted(result["counts"].items()):
        print(f"  {label}: {count}")
    return 0


def cmd_send_report(args):
    """Manually generates and sends one report to all configured admins right now --
    useful for testing report content/delivery without waiting for the schedule. Does NOT
    require reports_enabled (that flag only gates the AUTOMATIC per-iteration check inside
    telegram-bot-run); it DOES require the bot to be enabled and configured, same as
    telegram-bot-run, since it needs a working transport."""
    import reports as reports_mod
    import telegram_bot as tbot
    import telegram_config as tgc_mod

    settings = tgc_mod.load_telegram_settings(CONFIG_DIR)
    if not settings.enabled:
        print("BLOCKED: Telegram bot is not enabled.")
        return 1
    if not settings.admin_ids:
        print("BLOCKED: no admin_ids configured -- nothing to send to.")
        return 1
    try:
        secrets = tgc_mod.load_telegram_secrets(tgc_mod.default_secrets_path(CONFIG_DIR))
    except (FileNotFoundError, tgc_mod.TelegramConfigSecurityError) as e:
        print(f"BLOCKED: cannot load Telegram secrets: {e}")
        return 1
    try:
        transport = tbot.build_transport(settings, secrets)
    except ValueError as e:
        print(f"BLOCKED: cannot build transport: {e}")
        return 1

    report_text = reports_mod.generate_report_text(CONFIG_DIR, STATE_DIR,
                                                     period_label=getattr(args, "period", "Manual"))
    delivery = reports_mod.send_report_to_admins(transport, settings.admin_ids, report_text)
    for admin_id, result in delivery.items():
        status = "delivered" if result["ok"] else f"FAILED ({result['error']})"
        print(f"  admin {admin_id}: {status}")
    return 0 if all(r["ok"] for r in delivery.values()) else 1


def cmd_web_run(args):
    """Runs the Web Control Center (see web_server.py). Blocks forever (Type=simple,
    Restart=always -- the real deployment shape; no systemd unit template exists yet for
    this one, see docs/DEVELOPMENT_PROGRESS.md). Refuses to start unless explicitly enabled
    via web_config.WebSettings -- never auto-activates just because at least one user has
    been created."""
    import web_config as wcfg
    import web_server as websrv

    settings = wcfg.load_web_settings(CONFIG_DIR)
    if not settings.enabled:
        print("BLOCKED: Web Control Center is not enabled. Enable it via the menu's "
              "'Configure Web Control Center' screen first.")
        return 1
    scheme = "https" if (settings.tls_cert_path and settings.tls_key_path) else "http"
    print(f"Starting Web Control Center on {scheme}://{settings.bind_host}:{settings.bind_port} "
          f"(Ctrl+C to stop)...")
    try:
        server, _ = websrv.run_server_in_thread(CONFIG_DIR, STATE_DIR, settings.bind_host,
                                                  settings.bind_port, settings.tls_cert_path,
                                                  settings.tls_key_path)
    except websrv.TlsCertificateError as e:
        print(f"BLOCKED: {e}")
        return 1
    try:
        server.socket.settimeout(1.0)
        import time
        max_iterations = getattr(args, "max_iterations", None)
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            time.sleep(1)
            iterations += 1
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    print("Web Control Center stopped.")
    return 0


def cmd_relay_worker_run(args):
    """Runs the relay worker (see relay_worker.py) -- the SEPARATE process meant to run on a
    VPS that can reach Telegram, for an Iran-side host that cannot reach it directly. Uses
    its OWN config_dir/secrets (telegram_config.RelayWorkerSettings/RelayWorkerSecrets),
    never the Iran-side bot's settings. Refuses to start unless explicitly enabled."""
    import telegram_config as tc
    import relay_worker as rw

    settings = tc.load_relay_worker_settings(CONFIG_DIR)
    if not settings.enabled:
        print("BLOCKED: Relay worker is not enabled. Enable it via the menu's "
              "'Configure Relay Worker' screen first.")
        return 1
    try:
        secrets_obj = tc.load_relay_worker_secrets(tc.default_relay_worker_secrets_path(CONFIG_DIR))
    except (FileNotFoundError, tc.TelegramConfigSecurityError) as e:
        print(f"BLOCKED: {e}")
        return 1

    nonce_db_path = os.path.join(STATE_DIR, "relay_worker_nonces.sqlite3")
    print(f"Starting relay worker on {settings.bind_host}:{settings.bind_port} "
          f"(Ctrl+C to stop)...")
    server, _ = rw.run_server_in_thread(secrets_obj.bot_token, secrets_obj.shared_secret,
                                          nonce_db_path, settings.bind_host, settings.bind_port,
                                          settings.max_envelope_age_seconds)
    try:
        server.socket.settimeout(1.0)
        import time
        max_iterations = getattr(args, "max_iterations", None)
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            time.sleep(1)
            iterations += 1
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    print("Relay worker stopped.")
    return 0


def cmd_agent_run(args):
    """Runs the lightweight remote management agent (see agent_server.py) -- runs ON this
    host (local install mode is unaffected/unchanged), exposing a read-only, authenticated
    query surface for a separate central management server. Refuses to start unless
    explicitly enabled AND a shared secret is enrolled."""
    import agent_config as ac
    import agent_server as ags

    settings = ac.load_agent_settings(CONFIG_DIR)
    if not settings.enabled:
        print("BLOCKED: Remote agent is not enabled. Enable it via the menu's "
              "'Configure Remote Agent' screen first.")
        return 1
    try:
        secrets_obj = ac.load_agent_secrets(ac.default_secrets_path(CONFIG_DIR))
    except (FileNotFoundError, ac.AgentConfigSecurityError) as e:
        print(f"BLOCKED: {e}")
        return 1

    nonce_db_path = os.path.join(STATE_DIR, "agent_nonces.sqlite3")
    print(f"Starting remote agent on {settings.bind_host}:{settings.bind_port} "
          f"(Ctrl+C to stop)...")
    server, _ = ags.run_server_in_thread(CONFIG_DIR, STATE_DIR, secrets_obj.shared_secret,
                                          nonce_db_path, settings.bind_host, settings.bind_port,
                                          settings.max_envelope_age_seconds)
    try:
        server.socket.settimeout(1.0)
        import time
        max_iterations = getattr(args, "max_iterations", None)
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            time.sleep(1)
            iterations += 1
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    print("Remote agent stopped.")
    return 0


def cmd_telegram_bot_run(args):
    """Runs the Telegram long-polling loop (see telegram_bot.run_polling_loop). Blocks
    forever by default (the real deployment shape for a systemd service, Type=simple,
    Restart=always -- NOT a timer-triggered oneshot like hostnet-sync/inventory-sync,
    since this needs to hold a persistent long-poll connection) -- `--max-iterations`
    bounds it for a manual smoke-test run. Refuses to start at all unless the bot is
    explicitly enabled (`enabled: true` in telegram/settings.json, set via the menu's
    'Configure Telegram bot' screen) -- never auto-activates just because secrets happen
    to exist on disk."""
    import telegram_bot as tbot
    import telegram_config as tgc_mod
    import security as sec

    settings = tgc_mod.load_telegram_settings(CONFIG_DIR)
    if not settings.enabled:
        print("BLOCKED: Telegram bot is not enabled. Enable it via the menu's "
              "'Configure Telegram bot (optional)' screen first.")
        return 1
    try:
        secrets = tgc_mod.load_telegram_secrets(tgc_mod.default_secrets_path(CONFIG_DIR))
    except (FileNotFoundError, tgc_mod.TelegramConfigSecurityError) as e:
        print(f"BLOCKED: cannot load Telegram secrets: {e}")
        return 1
    try:
        transport = tbot.build_transport(settings, secrets)
    except ValueError as e:
        print(f"BLOCKED: cannot build transport: {e}")
        return 1

    rate_limit_conn = sec.open_rate_limit_db(os.path.join(STATE_DIR, "telegram-ratelimit.sqlite3"))
    max_iterations = getattr(args, "max_iterations", None)
    print(f"Starting Telegram bot polling loop (mode={settings.connectivity_mode}"
          f"{', max_iterations=' + str(max_iterations) if max_iterations else ''})...")
    result = tbot.run_polling_loop(transport, settings, CONFIG_DIR, STATE_DIR,
                                    rate_limit_conn=rate_limit_conn,
                                    max_iterations=max_iterations)
    print(f"Stopped: {result}")
    return 0


# Friendly public-CLI aliases (v1.10.6) -- rewritten to the real subcommand name BEFORE
# argparse ever sees them, so there is exactly one implementation of each (never a
# duplicated/divergent parser entry). `update` intentionally aliases to `update-apply`
# (not `update-check`) because update-apply already requires --yes or an interactive
# confirmation before doing anything -- `tixo-renewal update` run bare is exactly as safe
# as running `update-apply` bare, it just checks first and still asks before applying.
CLI_ALIASES = {"health-check": "install-health-check", "update": "update-apply"}


def _apply_cli_aliases(argv):
    if len(argv) > 1 and argv[1] in CLI_ALIASES:
        argv = list(argv)
        argv[1] = CLI_ALIASES[argv[1]]
    return argv


def main():
    sys.argv = _apply_cli_aliases(sys.argv)
    ap = argparse.ArgumentParser(prog="renewal_cli.py")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("install").set_defaults(func=cmd_install)
    sub.add_parser("install-detect", help="Read-only: report fresh vs. existing install state"
                   ).set_defaults(func=cmd_install_detect)
    sub.add_parser("install-upgrade", help="Backup first, then re-run idempotent install steps "
                   "and regenerate systemd units").set_defaults(func=cmd_install_upgrade)
    sub.add_parser("install-repair", help="Backup first, then recreate missing files/schemas "
                   "and fix loosened secret-file permissions").set_defaults(func=cmd_install_repair)

    install_rollback_p = sub.add_parser(
        "install-rollback",
        help="Restore config/state from a full backup archive (explicit recovery action)")
    install_rollback_p.add_argument("--backup-file", required=True)
    install_rollback_p.add_argument("--yes", action="store_true")
    install_rollback_p.set_defaults(func=cmd_install_rollback)

    sub.add_parser("install-health-check", help="Read-only post-change sanity check"
                   ).set_defaults(func=cmd_install_health_check)

    sub.add_parser(
        "readiness",
        help="Read-only: per-VPS operational-readiness summary (COLLECTING_BASELINE / "
             "MONITORING / REVIEW_REQUIRED / etc.) -- distinct from install-health-check's "
             "service-level health"
    ).set_defaults(func=cmd_readiness)

    sub.add_parser(
        "doctor",
        help="Read-only comprehensive self-diagnostic: service health, legacy-automation "
             "conflicts, credentials, backups, writer state, all in one human-readable report"
    ).set_defaults(func=cmd_doctor)

    logs_p = sub.add_parser("logs", help="Read-only: recent renewal operations and backups")
    logs_p.add_argument("--limit", type=int, default=20)
    logs_p.set_defaults(func=cmd_logs)

    sub.add_parser(
        "install-activate-core-timers",
        help="Root-only: install+enable the 4 read-only core timers into real systemd "
             "(monitor, inventory-sync, hostnet-sync, readiness-notifier)"
    ).set_defaults(func=cmd_install_activate_core_timers)

    enable_auto_p = sub.add_parser(
        "enable-auto-renewal",
        help="Create WRITE_APPROVED, set auto_renewal_enabled=true, and (if root) "
             "install+enable the write-capable apply timer -- all together")
    enable_auto_p.add_argument("--yes", action="store_true",
                                help="Skip the interactive confirmation (for scripted use)")
    enable_auto_p.add_argument(
        "--override-legacy-write-conflict", action="store_true",
        help="Proceed even if a foreign, write-capable bandwidth automation cron job is "
             "detected already active on this host (never disables/modifies that job)")
    enable_auto_p.set_defaults(func=cmd_enable_auto_renewal)

    sub.add_parser(
        "disable-auto-renewal",
        help="Remove WRITE_APPROVED, set auto_renewal_enabled=false, and (if root) "
             "stop+disable+remove the apply timer -- always all together"
    ).set_defaults(func=cmd_disable_auto_renewal)

    sub.add_parser("update-check", help="Read-only: current vs. latest GitHub release"
                   ).set_defaults(func=cmd_update_check)

    update_apply_p = sub.add_parser(
        "update-apply", help="Download, verify, and apply a release (backs up first, "
                              "rolls back automatically on any failure)")
    update_apply_p.add_argument("--version", default=None,
                                 help="Specific version to apply (e.g. 1.10.2); default: latest")
    update_apply_p.add_argument("--yes", action="store_true",
                                 help="Skip the interactive confirmation (for scripted use)")
    update_apply_p.add_argument("--force", action="store_true",
                                 help="Apply even if the target is not newer than the installed version")
    update_apply_p.set_defaults(func=cmd_update_apply)

    uninstall_module_p = sub.add_parser(
        "uninstall-module",
        help="Safely disable ONE optional module without touching the core install")
    uninstall_module_p.add_argument("--module", required=True,
                                     choices=("telegram", "web", "relay_worker", "agent",
                                              "backup_schedule"))
    uninstall_module_p.add_argument("--purge-secrets", action="store_true")
    uninstall_module_p.set_defaults(func=cmd_uninstall_module)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("dry-run").set_defaults(func=cmd_dry_run)
    sub.add_parser("verify").set_defaults(func=cmd_verify)
    sub.add_parser("backup").set_defaults(func=cmd_backup)

    backup_full_p = sub.add_parser("backup-full")
    backup_full_p.add_argument("--label")
    backup_full_p.add_argument("--encrypt", action="store_true")
    backup_full_p.add_argument("--keep", type=int, default=None)
    backup_full_p.set_defaults(func=cmd_backup_full)

    sub.add_parser("backup-scheduled-run",
                    help="Entry point for the optional scheduled-backup timer (reads "
                         "backup_config.BackupScheduleSettings; requires enabling via the "
                         "menu's 'Configure Backup Schedule' screen first)").set_defaults(
        func=cmd_backup_scheduled_run)

    rollback_p = sub.add_parser("rollback")
    rollback_p.add_argument("--node")
    rollback_p.add_argument("--backup-file")
    rollback_p.add_argument("--yes", action="store_true")
    rollback_p.set_defaults(func=cmd_rollback)

    uninstall_preview_p = sub.add_parser(
        "uninstall-preview", help="Read-only: exactly what 'uninstall'/'uninstall --purge' "
                                   "would do, without doing it")
    uninstall_preview_p.add_argument("--purge", action="store_true")
    uninstall_preview_p.set_defaults(func=cmd_uninstall_preview)

    uninstall_p = sub.add_parser("uninstall")
    uninstall_p.add_argument("--purge", action="store_true",
                              help="Full purge: also removes config/secrets/databases/ledgers "
                                   "(never backups/ -- see --delete-backups). Requires typing "
                                   "'PURGE ALL TIXOCLOUD DATA' (or --confirm with --yes).")
    uninstall_p.add_argument("--confirm", default=None,
                              help="Must exactly equal 'PURGE ALL TIXOCLOUD DATA' when "
                                   "used with --purge --yes (scripted/tested use)")
    uninstall_p.add_argument("--delete-backups", action="store_true",
                              help="ALSO delete install_dir/backups/ -- separate from, and "
                                   "requires its own confirmation beyond, --purge")
    uninstall_p.add_argument("--yes", action="store_true",
                              help="Skip interactive confirmation prompts (for scripted use)")
    uninstall_p.set_defaults(func=cmd_uninstall)

    sub.add_parser("policy-show").set_defaults(func=cmd_policy_show)
    sub.add_parser("policy-validate").set_defaults(func=cmd_policy_validate)

    preview_p = sub.add_parser("preview")
    preview_p.add_argument("--node", required=True)
    preview_p.add_argument("--vpsid", required=True, type=int)
    preview_p.set_defaults(func=cmd_preview)

    apply_p = sub.add_parser("apply")
    apply_p.add_argument("--node")
    apply_p.add_argument("--vpsid")
    apply_p.add_argument("--period-key")
    apply_p.add_argument("--new-quota-gb")
    apply_p.add_argument("--execute", action="store_true")
    apply_p.add_argument("--non-interactive", action="store_true")
    apply_p.set_defaults(func=cmd_apply)

    store_creds_p = sub.add_parser("store-credentials")
    store_creds_p.add_argument("--node", required=True)
    store_creds_p.set_defaults(func=cmd_store_credentials)

    validate_creds_p = sub.add_parser("validate-credentials")
    validate_creds_p.add_argument("--node", required=True)
    validate_creds_p.set_defaults(func=cmd_validate_credentials)

    diagnose_p = sub.add_parser("diagnose-api")
    diagnose_p.add_argument("--node", required=True)
    diagnose_p.set_defaults(func=cmd_diagnose_api)

    inspect_p = sub.add_parser("inspect-vps")
    inspect_p.add_argument("--node", required=True)
    inspect_p.add_argument("--vpsid", required=True, type=int)
    inspect_p.set_defaults(func=cmd_inspect_vps)

    apply_due_p = sub.add_parser("apply-due")
    apply_due_p.add_argument("--node", required=True)
    apply_due_p.add_argument("--non-interactive", action="store_true")
    apply_due_p.set_defaults(func=cmd_apply_due)

    install_ui_p = sub.add_parser("install-ui")
    install_ui_p.add_argument("--yes", action="store_true")
    install_ui_p.set_defaults(func=cmd_install_ui)

    check_exhausted_p = sub.add_parser(
        "check-exhausted",
        help="Evaluate the already-synced VPS inventory against the configured "
             "exhausted-bandwidth policy (no Virtualizor API call; no-op if the policy is "
             "disabled)")
    check_exhausted_p.add_argument("--node")
    check_exhausted_p.set_defaults(func=cmd_check_exhausted)

    send_report_p = sub.add_parser(
        "send-report",
        help="Manually generate and send one report to all configured Telegram admins now "
             "(for testing; does not require reports_enabled)")
    send_report_p.add_argument("--period", default="Manual",
                                help="Display label for the report (e.g. Daily/Weekly/Manual)")
    send_report_p.set_defaults(func=cmd_send_report)

    telegram_bot_run_p = sub.add_parser(
        "telegram-bot-run",
        help="Run the Telegram bot long-polling loop (blocks; requires the bot to be "
             "enabled via the menu's 'Configure Telegram bot' screen first)")
    telegram_bot_run_p.add_argument(
        "--max-iterations", type=int, default=None,
        help="Bound the loop to N getUpdates iterations then exit (for manual smoke-testing; "
             "omit for the real deployment shape, which runs forever)")
    telegram_bot_run_p.set_defaults(func=cmd_telegram_bot_run)

    web_run_p = sub.add_parser(
        "web-run",
        help="Run the Web Control Center (blocks; requires enabling via the menu's "
             "'Configure Web Control Center' screen first)")
    web_run_p.add_argument("--max-iterations", type=int, default=None,
                            help="Bound the run to N one-second ticks then exit (manual testing)")
    web_run_p.set_defaults(func=cmd_web_run)

    relay_worker_run_p = sub.add_parser(
        "relay-worker-run",
        help="Run the relay worker (blocks; runs on a SEPARATE host that can reach "
             "Telegram; requires enabling via the menu's 'Configure Relay Worker' screen "
             "first, on THAT host)")
    relay_worker_run_p.add_argument("--max-iterations", type=int, default=None,
                                     help="Bound the run to N one-second ticks then exit "
                                          "(manual testing)")
    relay_worker_run_p.set_defaults(func=cmd_relay_worker_run)

    agent_run_p = sub.add_parser(
        "agent-run",
        help="Run the lightweight remote management agent (blocks; read-only, requires "
             "enabling via the menu's 'Configure Remote Agent' screen first)")
    agent_run_p.add_argument("--max-iterations", type=int, default=None,
                              help="Bound the run to N one-second ticks then exit (manual testing)")
    agent_run_p.set_defaults(func=cmd_agent_run)

    sub.add_parser("hostinfo").set_defaults(func=cmd_hostinfo)

    vinfo_p = sub.add_parser("virtualizor-info")
    vinfo_p.add_argument("--node")
    vinfo_p.set_defaults(func=cmd_virtualizor_info)

    inv_sync_p = sub.add_parser("inventory-sync")
    inv_sync_p.add_argument("--node", required=True)
    inv_sync_p.set_defaults(func=cmd_inventory_sync)

    inv_status_p = sub.add_parser("inventory-status")
    inv_status_p.add_argument("--node", required=True)
    inv_status_p.set_defaults(func=cmd_inventory_status)

    hostnet_configure_p = sub.add_parser("hostnet-configure")
    hostnet_configure_p.add_argument("--interface", required=True)
    hostnet_configure_p.add_argument("--yes", action="store_true")
    hostnet_configure_p.set_defaults(func=cmd_hostnet_configure)

    sub.add_parser("hostnet-suggest").set_defaults(func=cmd_hostnet_suggest)
    sub.add_parser("hostnet-sync").set_defaults(func=cmd_hostnet_sync)
    sub.add_parser("hostnet-status").set_defaults(func=cmd_hostnet_status)

    resolve_p = sub.add_parser("resolve-operation")
    resolve_p.add_argument("--node", required=True)
    resolve_p.add_argument("--mark-resolved", help="operation_id to resolve")
    resolve_p.add_argument("--resolution", choices=["APPLIED", "FAILED"])
    resolve_p.add_argument("--verified-quota-gb")
    resolve_p.set_defaults(func=cmd_resolve_operation)

    args = ap.parse_args()
    try:
        rc = args.func(args)
    except Exception as e:
        # Final, independent safety net for EVERY command (not just credential-related
        # ones): never let Python's default uncaught-exception traceback reach the
        # terminal/logs verbatim. writer.py already sanitizes known credential-bearing
        # exceptions at the source; this generic pattern-based redaction is a second layer
        # for anything unanticipated, since it doesn't need the actual secret value in scope.
        import writer as writer_mod
        print(f"ERROR: {writer_mod.redact_url_patterns(str(e))}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
