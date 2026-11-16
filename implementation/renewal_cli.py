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

VERSION = "1.4.0"

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


def write_systemd_units(monitor_interval_min: int = DEFAULT_MONITOR_INTERVAL_MIN,
                         apply_interval_min: int = DEFAULT_APPLY_INTERVAL_MIN,
                         notifier_interval_min: int = DEFAULT_NOTIFIER_INTERVAL_MIN) -> None:
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
    write_systemd_units(monitor_min, apply_min, notifier_min)
    service_path = os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-monitor.service")
    timer_path = os.path.join(SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer")

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


def cmd_rollback(args):
    if not args.backup_file:
        print("Available backups:")
        for f in backup_mod.list_backups(BACKUP_DIR):
            print(f"  {f}")
        print("\nUsage: renewal_cli.py rollback --node <node_id> --backup-file <filename> --yes")
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


# ---------- uninstall ----------

UNINSTALL_UNIT_NAMES = (
    "virt-renewal-monitor.service", "virt-renewal-monitor.timer",
    "virt-renewal-apply.service", "virt-renewal-apply.timer",
    "virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer",
)


def cmd_uninstall(args):
    for name in UNINSTALL_UNIT_NAMES:
        local_path = os.path.join(SYSTEMD_OUT_DIR, name)
        if os.path.exists(local_path):
            os.remove(local_path)
    print("Local systemd unit files removed from", SYSTEMD_OUT_DIR)
    print("If any were installed system-wide, also run (as root, your own explicit action --")
    print("the apply timer is write-capable, disable it FIRST if it was ever enabled):")
    print("  sudo systemctl disable --now virt-renewal-apply.timer virt-renewal-monitor.timer virt-renewal-readiness-notifier.timer")
    print("  sudo rm /etc/systemd/system/virt-renewal-{apply,monitor,readiness-notifier}.{service,timer}")
    print("  sudo systemctl daemon-reload")
    print("  rm -f " + WRITE_APPROVAL_MARKER + "  # belt-and-suspenders: ensure no write capability survives uninstall")
    if args.purge:
        import shutil
        for d in (STATE_DIR, CONFIG_DIR):
            if os.path.exists(d):
                shutil.rmtree(d)
        print(f"--purge: removed {STATE_DIR} and {CONFIG_DIR}")
    else:
        print("Local ledger/config data kept (pass --purge to remove it too).")


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
    the (unenabled) unattended systemd timer would run. Still requires WRITE_APPROVED; with
    --non-interactive it also requires credentials already stored via `store-credentials`."""
    if not os.path.exists(WRITE_APPROVAL_MARKER):
        print("BLOCKED: production write capability is not approved.")
        print(f"To enable it, the OPERATOR must explicitly create: {WRITE_APPROVAL_MARKER}")
        return 1

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
    renewal engine's own apply path."""
    import inventory as inv_mod
    import inventory_sync as isync_mod
    path = creds_mod.default_credentials_path(CONFIG_DIR, args.node)
    try:
        stored = creds_mod.load_credentials(path)
    except (FileNotFoundError, creds_mod.CredentialSecurityError) as e:
        print(f"Cannot load stored credentials: {e}")
        return 1
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


def main():
    ap = argparse.ArgumentParser(prog="renewal_cli.py")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("install").set_defaults(func=cmd_install)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("dry-run").set_defaults(func=cmd_dry_run)
    sub.add_parser("verify").set_defaults(func=cmd_verify)
    sub.add_parser("backup").set_defaults(func=cmd_backup)

    rollback_p = sub.add_parser("rollback")
    rollback_p.add_argument("--node")
    rollback_p.add_argument("--backup-file")
    rollback_p.add_argument("--yes", action="store_true")
    rollback_p.set_defaults(func=cmd_rollback)

    uninstall_p = sub.add_parser("uninstall")
    uninstall_p.add_argument("--purge", action="store_true")
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
