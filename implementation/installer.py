"""Unified installer support functions (v1.8.0 Part 17) -- the logic behind fresh
install/upgrade/repair/reconfigure/rollback, shared by both the interactive menu wizard
(`menu.action_install_wizard`) and noninteractive CLI commands (`renewal_cli.py
install-detect/install-upgrade/install-repair/install-rollback`), so there is exactly one
implementation of each operation, never two to keep in sync -- the same discipline already
used for `write_systemd_units()`.

SAFETY: every function here operates purely on local files/sockets. Nothing here ever
changes the host's actual system timezone, touches host networking, opens a firewall port,
or restarts a running systemd unit -- generating/validating files and printing the exact
manual commands an operator must run themselves is the limit of what this module does,
matching every other optional-module config screen already built in this project.
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path


# ---------- global wrapper script (v1.10.7) -----------------------------------------------------

# CONFIRMED REAL PRODUCTION REGRESSION (found on the actual IR1 host, 2026-11-24, during a
# real v1.10.5 -> v1.10.6 upgrade via `update-apply`): the global `/usr/local/bin/tixo-renewal`
# wrapper script's dispatch logic (no args / --status / --version / --cli -> menu.py,
# anything else -> renewal_cli.py) previously existed ONLY as a bash heredoc inside
# deploy/install.sh -- a fresh install wrote it once, but update-apply's entire upgrade path
# is pure Python (download/verify/backup/swap lib+bin/run installer.upgrade()/health-check)
# and NEVER called into install.sh again, so the wrapper script on disk was simply never
# touched by any upgrade. On IR1, `renewal_cli.VERSION` correctly reported "1.10.6" (the
# Python code really was upgraded) while the wrapper script's mtime and content were still
# from the ORIGINAL v1.10.5 fresh install -- `tixo-renewal doctor` kept launching the
# interactive menu because the only wrapper that existed on disk still unconditionally did
# exactly that, regardless of what version of menu.py/renewal_cli.py it happened to invoke.
#
# Fix: this is now the ONE place the wrapper's content is ever generated, called from BOTH
# cmd_install() (fresh install, replacing install.sh's own former bash heredoc entirely --
# no second implementation to drift out of sync again) AND installer.upgrade() (every
# upgrade path: install-upgrade, the menu's install wizard, and update-apply's
# run_install_upgrade_fn, which all go through upgrade() -> cmd_install() already).
GLOBAL_WRAPPER_PATH = "/usr/local/bin/tixo-renewal"

GLOBAL_WRAPPER_TEMPLATE = """#!/bin/bash
export TIXO_RENEWAL_INSTALL_DIR="{install_dir}"
export PYTHONPATH="{install_dir}/lib:{install_dir}/bin"
# menu.py itself already handles --status/--version/--cli as special noninteractive flags
# (documented, pre-existing, tested) -- those three, and the no-argument case, go to
# menu.py. Any OTHER argument is a real renewal_cli.py subcommand name (status, backup,
# install-health-check, readiness, doctor, logs, enable-auto-renewal, ...).
case "${{1:-}}" in
  ""|"--status"|"--version"|"--cli")
    exec "{python_bin}" "{install_dir}/lib/menu.py" "$@"
    ;;
  *)
    exec "{python_bin}" "{install_dir}/bin/renewal_cli.py" "$@"
    ;;
esac
"""


def write_global_wrapper(install_dir: str, python_bin: str = None,
                           wrapper_path: str = None) -> dict:
    """(Re)writes the global `tixo-renewal` wrapper script -- idempotent, always overwrites
    with the CURRENT correct content regardless of what (if anything) was there before, so
    an upgrade can never leave a stale wrapper behind the way the real regression above did.
    Never fatal if the target directory isn't writable (e.g. not root) -- returns
    {"written": False, "reason": ...} instead of raising, matching this module's existing
    convention for every other installer step that requires root.

    `wrapper_path`, if not given, falls back to the TIXO_RENEWAL_WRAPPER_PATH environment
    variable (checked fresh here, every call -- not an early-bound default) before
    GLOBAL_WRAPPER_PATH -- this is what lets a test (or an operator with a non-standard
    prefix) redirect the real default WITHOUT touching the real host's
    /usr/local/bin/tixo-renewal, including when this function runs inside a genuinely
    separate subprocess (as updater.apply_update's post-update install step does) where an
    in-process monkeypatch of GLOBAL_WRAPPER_PATH could never reach it -- only an inherited
    environment variable can."""
    import sys as _sys
    wrapper_path = wrapper_path or os.environ.get("TIXO_RENEWAL_WRAPPER_PATH") or GLOBAL_WRAPPER_PATH
    python_bin = python_bin or _sys.executable
    target_dir = os.path.dirname(wrapper_path)
    if not (os.access(target_dir, os.W_OK) or os.geteuid() == 0):
        return {"written": False,
                "reason": f"{target_dir} is not writable and this process is not root"}
    content = GLOBAL_WRAPPER_TEMPLATE.format(
        install_dir=os.path.abspath(install_dir), python_bin=python_bin)
    tmp = wrapper_path + ".tmp"
    Path(tmp).write_text(content)
    os.chmod(tmp, 0o755)
    os.replace(tmp, wrapper_path)
    return {"written": True, "path": wrapper_path}


# ---------- installation-state detection --------------------------------------------------------

def detect_installation_state(config_dir: str, state_dir: str) -> dict:
    """Read-only inspection of whether this looks like a fresh install or an existing one.
    Never writes anything. Used to choose between the fresh-install and upgrade/repair paths
    without guessing or assuming."""
    nodes_path = os.path.join(config_dir, "nodes.json")
    policy_path = os.path.join(config_dir, "policy.json")
    has_nodes = os.path.exists(nodes_path)
    has_policy = os.path.exists(policy_path)
    db_files = []
    if os.path.isdir(state_dir):
        db_files = sorted(f for f in os.listdir(state_dir) if f.endswith(".sqlite3"))
    return {
        "existing": has_nodes or has_policy or bool(db_files),
        "has_nodes": has_nodes,
        "has_policy": has_policy,
        "db_files": db_files,
        "config_dir": config_dir,
        "state_dir": state_dir,
    }


# ---------- legacy / foreign bandwidth-automation detection (v1.10.5) ---------------------------

# Found during a real production audit (ir1/ir2, 2026-11-24): a standalone, pre-existing
# script-based bandwidth carry-over system (NOT part of this project) was already installed
# via /etc/cron.d on both hosts during prior troubleshooting -- monitor/ledger/guard scripts
# that are read-only (never write to Virtualizor), plus one script, matching the "auto"
# pattern below, that CAN write directly to Virtualizor's MySQL `vps.bandwidth` column. This
# project must never silently assume it is the only thing managing bandwidth on a host, and
# must never let an operator enable ITS OWN write-capable timer while a foreign write-capable
# one might also be active -- that is exactly how a renewal could be double-applied by two
# independent, mutually-unaware systems. Per explicit instruction, this project NEVER
# disables, deletes, or modifies anything matching these patterns -- detection only.
LEGACY_CRON_DIR = "/etc/cron.d"
LEGACY_CARRYOVER_PATTERNS = ("virt-carryover-monitor", "virt-carryover-ledger",
                              "virt-carryover-guard", "virt-carryover-auto",
                              "virt-bandwidth-topup", "virt-customer-topup")
# A legacy cron filename containing any of these substrings is treated as write-capable for
# the purposes of this detector's "active_write_capable" flag -- read-only monitor/ledger/
# guard scripts are reported too, but do not themselves trigger the stronger warning.
LEGACY_WRITE_CAPABLE_SUBSTRINGS = ("auto", "topup", "apply", "fix")


def detect_legacy_bandwidth_automation(cron_dir: str = None) -> dict:
    """Read-only scan of `cron_dir` (default: the current value of module-level
    LEGACY_CRON_DIR, resolved HERE at call time rather than bound as an early default
    parameter value -- a `cron_dir=LEGACY_CRON_DIR` default would bind to whatever
    LEGACY_CRON_DIR was at function-DEFINITION time, so a test monkeypatching the module
    attribute afterward would have no effect on calls relying on the default; see
    updater.apply_update's identical fix earlier in this project's history for the exact
    same gotcha) for any file whose name matches a known legacy/foreign bandwidth-automation
    pattern -- never reads file CONTENTS (that would require trusting arbitrary foreign
    script text), never modifies anything found. A cron.d entry is considered genuinely
    active if the file exists and its name does not end in `.disabled` (the convention this
    project observed the real legacy `virt-carryover-auto` job using when it was
    deliberately turned off) -- a `.disabled`-suffixed file is reported as `found_disabled`,
    not `found_active`, so an operator can tell at a glance whether a write-capable legacy
    job is currently armed."""
    if cron_dir is None:
        cron_dir = LEGACY_CRON_DIR
    report = {"found_active": [], "found_disabled": [], "active_write_capable": False}
    if not os.path.isdir(cron_dir):
        return report
    for name in sorted(os.listdir(cron_dir)):
        base = name[:-len(".disabled")] if name.endswith(".disabled") else name
        if not any(pattern in base for pattern in LEGACY_CARRYOVER_PATTERNS):
            continue
        if name.endswith(".disabled"):
            report["found_disabled"].append(name)
        else:
            report["found_active"].append(name)
            if any(s in name for s in LEGACY_WRITE_CAPABLE_SUBSTRINGS):
                report["active_write_capable"] = True
    return report


# ---------- port-conflict detection ---------------------------------------------------------------

def check_port_available(host: str, port: int) -> bool:
    """A real bind-and-immediately-close probe -- not a guess based on config. Returns True
    if the port is free to bind on `host` right now. Never leaves a socket open; never
    touches firewall rules."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------- HTTPS prerequisite validation ---------------------------------------------------------

def validate_https_prerequisites(tls_cert_path: str, tls_key_path: str, bind_host: str,
                                   bind_port: int) -> dict:
    """Checks everything that can be checked LOCALLY and SAFELY before an operator enables
    HTTPS with manual certificate paths: both files exist and are readable, the cert/key
    pair actually loads together (catches a mismatched pair before a confusing runtime
    failure), and the target port is currently free. Never makes a network call (no DNS
    resolution, no CA validation) -- this is a local readiness check, not a trust
    assertion."""
    report = {"ok": True, "problems": []}

    if not tls_cert_path or not tls_key_path:
        report["ok"] = False
        report["problems"].append("both tls_cert_path and tls_key_path must be provided")
        return report

    if not os.path.exists(tls_cert_path):
        report["ok"] = False
        report["problems"].append(f"certificate file not found: {tls_cert_path}")
    if not os.path.exists(tls_key_path):
        report["ok"] = False
        report["problems"].append(f"key file not found: {tls_key_path}")
    if not report["ok"]:
        return report

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=tls_cert_path, keyfile=tls_key_path)
    except (ssl.SSLError, OSError) as e:
        report["ok"] = False
        report["problems"].append(f"certificate/key did not load together: {e}")

    if not check_port_available(bind_host, bind_port):
        report["ok"] = False
        report["problems"].append(f"port {bind_port} on {bind_host} is already in use")

    return report


# ---------- automatic certificate provisioning (explicit-trigger only) ---------------------------

def request_automatic_certificate(domain: str, email: str, webroot_path: str,
                                    certbot_bin: str = None, run_fn=None) -> dict:
    """Checks whether automatic issuance via certbot's webroot plugin is SAFELY POSSIBLE,
    and only actually attempts it if the caller explicitly invokes this function (this
    module never calls it on its own, and the installer wizard only reaches it after an
    explicit operator confirmation). NEVER opens a firewall port or changes host
    networking -- the operator is responsible for ensuring port 80 is already reachable for
    the webroot challenge, which this function only CHECKS, never arranges.
    `run_fn` is injectable for tests (same DI convention as writer.py's `http_post`); real
    usage defaults to a genuine `certbot certonly` subprocess call with list-args only."""
    certbot_bin = certbot_bin or shutil.which("certbot")
    if not certbot_bin:
        return {"ok": False, "attempted": False,
                "error": "certbot is not installed on this host -- automatic certificate "
                         "provisioning is not safely possible; use manual certificate paths"}
    if not check_port_available("0.0.0.0", 80):
        return {"ok": False, "attempted": False,
                "error": "port 80 is already in use -- certbot's webroot challenge needs it "
                         "free; automatic provisioning is not safely possible right now"}
    if not os.path.isdir(webroot_path):
        return {"ok": False, "attempted": False,
                "error": f"webroot path does not exist: {webroot_path}"}

    cmd = [certbot_bin, "certonly", "--webroot", "-w", webroot_path, "-d", domain,
           "-m", email, "--agree-tos", "--non-interactive"]
    if run_fn is None:
        def run_fn(cmd):
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    result = run_fn(cmd)
    ok = getattr(result, "returncode", 1) == 0
    return {"ok": ok, "attempted": True,
            "error": None if ok else getattr(result, "stderr", "certbot failed")}


# ---------- backup-before-change / rollback -------------------------------------------------------

def backup_before_change(config_dir: str, state_dir: str, backup_root: str,
                           label: str = "pre-change") -> str:
    """Thin, explicitly-named wrapper around backup.create_full_backup_archive() -- every
    upgrade/repair below calls this FIRST, before touching anything, so a rollback target
    always exists."""
    import backup as backup_mod
    return backup_mod.create_full_backup_archive(state_dir, config_dir, backup_root, label=label)


def rollback(archive_path: str, config_dir: str, state_dir: str) -> dict:
    """Restores config/state from a full backup archive -- this IS a destructive action by
    design (it is explicitly invoked only to recover from a FAILED upgrade/repair), but it
    is never silent: the CURRENT state is itself safety-copied first (mirroring
    backup.restore_db's existing `.pre-restore` discipline), and every database is integrity-
    verified via backup.restore_full_backup_archive() BEFORE anything live is touched.
    Returns a report of what was restored."""
    import backup as backup_mod
    import tempfile

    with tempfile.TemporaryDirectory() as staging:
        extracted = backup_mod.restore_full_backup_archive(archive_path, staging)

        safety_label = "pre-rollback"
        safety_archive = None
        if os.path.isdir(state_dir) and os.listdir(state_dir):
            safety_archive = backup_before_change(
                config_dir, state_dir, os.path.join(state_dir, "..", "backups"),
                label=safety_label)

        db_dir = os.path.join(staging, "databases")
        restored_dbs = []
        for db_name in extracted["databases"]:
            dest = os.path.join(state_dir, db_name)
            shutil.copy2(os.path.join(db_dir, db_name), dest)
            restored_dbs.append(db_name)

        cfg_dir = os.path.join(staging, "config")
        restored_config = []
        for rel_path in extracted["config"]:
            src = os.path.join(cfg_dir, rel_path)
            dest = os.path.join(config_dir, rel_path)
            Path(os.path.dirname(dest)).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored_config.append(rel_path)

        return {"restored_databases": restored_dbs, "restored_config": restored_config,
                "safety_backup": safety_archive}


# ---------- upgrade / repair -----------------------------------------------------------------------

def upgrade(config_dir: str, state_dir: str, backup_root: str) -> dict:
    """Idempotent, config-preserving upgrade: backs up first, then re-runs exactly the same
    directory/default-config/schema creation `cmd_install` already does (all of which is
    itself idempotent -- CREATE TABLE IF NOT EXISTS, write-default-only-if-absent), then
    regenerates every systemd unit template. Never overwrites an existing nodes.json/
    policy.json -- those are exactly the "existing Virtualizor renewal configuration" this
    is required to preserve."""
    backup_path = backup_before_change(config_dir, state_dir, backup_root, label="pre-upgrade")

    import renewal_cli
    import argparse
    prev_config_dir, prev_state_dir = renewal_cli.CONFIG_DIR, renewal_cli.STATE_DIR
    renewal_cli.CONFIG_DIR, renewal_cli.STATE_DIR = config_dir, state_dir
    try:
        rc = renewal_cli.cmd_install(argparse.Namespace())
    finally:
        renewal_cli.CONFIG_DIR, renewal_cli.STATE_DIR = prev_config_dir, prev_state_dir

    return {"ok": rc in (0, None), "backup": backup_path}


def repair(config_dir: str, state_dir: str, backup_root: str) -> dict:
    """Recreates missing directories/files/schemas WITHOUT touching anything that already
    exists and is valid, and fixes any 0600-secret file whose permissions have been loosened
    (never widens permissions, only ever tightens them back to 0600). Backs up first, same
    as upgrade(). Returns a report of exactly what was fixed."""
    backup_path = backup_before_change(config_dir, state_dir, backup_root, label="pre-repair")

    fixed_permissions = []
    for root, _dirs, files in os.walk(config_dir):
        for fname in files:
            if fname in ("secrets.json", "csrf_secret.key", "passphrase.key", "users.json"):
                full = os.path.join(root, fname)
                import stat
                mode = stat.S_IMODE(os.stat(full).st_mode)
                if mode & 0o077:
                    os.chmod(full, 0o600)
                    fixed_permissions.append(full)

    upgrade_result = upgrade(config_dir, state_dir, backup_root)

    return {"ok": upgrade_result["ok"], "backup": backup_path,
            "fixed_permissions": fixed_permissions}


# ---------- post-change health check ----------------------------------------------------------

# ---------- safe uninstall of a single optional module -------------------------------------------

OPTIONAL_MODULES = ("telegram", "web", "relay_worker", "agent", "backup_schedule")


def uninstall_optional_module(module_name: str, config_dir: str, systemd_out_dir: str,
                                purge_secrets: bool = False) -> dict:
    """Disables ONE optional module (sets its settings.enabled = False) without touching
    the core renewal install, any other optional module, or any historical data. Optionally
    (only if explicitly requested) also deletes that module's stored secret file --
    disabled by default so an operator can re-enable without re-entering credentials.
    Removes that module's generated-but-never-installed systemd unit file(s) from
    `systemd_out_dir` -- this NEVER touches `/etc/systemd/system` or runs `systemctl`
    itself; if the unit was ever actually installed/enabled there, the operator must still
    run the printed `systemctl disable --now ...` command themselves."""
    if module_name not in OPTIONAL_MODULES:
        raise ValueError(f"unknown module: {module_name!r} (must be one of {OPTIONAL_MODULES})")

    report = {"module": module_name, "disabled": False, "secrets_purged": False,
               "units_removed": []}

    if module_name == "telegram":
        import telegram_config as tc
        settings = tc.load_telegram_settings(config_dir)
        settings.enabled = False
        tc.save_telegram_settings(config_dir, settings)
        if purge_secrets:
            tc.delete_telegram_secrets(tc.default_secrets_path(config_dir))
        units = ("virt-renewal-telegram-bot.service",)
    elif module_name == "web":
        import web_config as wc
        settings = wc.load_web_settings(config_dir)
        settings.enabled = False
        wc.save_web_settings(config_dir, settings)
        if purge_secrets:
            import shutil as _shutil
            users_dir = os.path.join(config_dir, "web")
            if os.path.isdir(users_dir):
                _shutil.rmtree(users_dir)
        units = ("virt-renewal-web.service",)
    elif module_name == "relay_worker":
        import telegram_config as tc
        settings = tc.load_relay_worker_settings(config_dir)
        settings.enabled = False
        tc.save_relay_worker_settings(config_dir, settings)
        if purge_secrets:
            path = tc.default_relay_worker_secrets_path(config_dir)
            if os.path.exists(path):
                os.remove(path)
        units = ("virt-renewal-relay-worker.service",)
    elif module_name == "agent":
        import agent_config as ac
        settings = ac.load_agent_settings(config_dir)
        settings.enabled = False
        ac.save_agent_settings(config_dir, settings)
        if purge_secrets:
            path = ac.default_secrets_path(config_dir)
            if os.path.exists(path):
                os.remove(path)
        units = ("virt-renewal-agent.service",)
    else:  # backup_schedule
        import backup_config as bcfg
        settings = bcfg.load_backup_schedule_settings(config_dir)
        settings.enabled = False
        bcfg.save_backup_schedule_settings(config_dir, settings)
        if purge_secrets:
            path = bcfg.default_passphrase_path(config_dir)
            if os.path.exists(path):
                os.remove(path)
        units = ("virt-renewal-backup.service", "virt-renewal-backup.timer")

    report["disabled"] = True
    report["secrets_purged"] = purge_secrets
    for unit in units:
        unit_path = os.path.join(systemd_out_dir, unit)
        if os.path.exists(unit_path):
            os.remove(unit_path)
            report["units_removed"].append(unit)

    return report


# ---------- unified automatic-renewal / production-write activation (v1.10.3) ------------------

WRITE_APPROVAL_MARKER_NAME = "WRITE_APPROVED"


def enable_auto_renewal(config_dir: str, systemd_src_dir: str, activate_timer: bool = True,
                         override_legacy_write_conflict: bool = False) -> dict:
    """The single, real implementation behind BOTH `renewal_cli.cmd_enable_auto_renewal` and
    `menu.action_toggle_auto_renewal(True)` -- confirmation is always the CALLER's job (this
    function performs no interactive prompting itself, matching every other
    destructive-ish action in this project), but every actual state change that "enabling
    automatic renewal" is supposed to mean happens HERE, in one place, so the CLI and menu
    can never drift into doing different subsets of it.

    Before v1.10.3, 'enable automatic renewal' only ever flipped
    policy.json.global_defaults.auto_renewal_enabled -- it never created WRITE_APPROVED
    (the operator had to do that by hand, with no guidance on WHEN), and it never touched
    the `apply` timer at all (an operator who separately found and enabled that timer could
    end up with a running write-capable service despite never having gone through this
    confirmation). This function now does all three required steps together:
      1. Validates nodes.json/policy.json load cleanly (refuses -- returns ok=False --
         rather than enabling writes against a config known to be broken).
      1.5. (v1.10.5) Refuses -- returns ok=False -- if detect_legacy_bandwidth_automation()
         finds an ACTIVE, write-capable, foreign cron job already managing bandwidth on this
         host (found during a real production audit: a standalone script-based carry-over
         system, installed independently of this project, that CAN write directly to
         Virtualizor's database). Two independently-unaware automatic writers is exactly how
         a renewal gets double-applied. This project never touches that foreign job itself
         -- the operator must either confirm it is safe to proceed anyway
         (`override_legacy_write_conflict=True`, e.g. because the foreign job is being
         retired deliberately) or resolve the conflict out-of-band first.
      2. Creates WRITE_APPROVAL_MARKER_NAME if not already present.
      3. Sets policy.json's auto_renewal_enabled = True.
      4. If `activate_timer` and running as root: installs+enables the real, write-capable
         `virt-renewal-apply` timer (the ONLY unit this project ever auto-activates that can
         write to Virtualizor -- see service_activation.WRITE_CAPABLE_UNIT's docstring for
         why this is kept structurally separate from the four read-only core timers).
         If not root, this step is skipped and reported as such -- config-level
         authorization still succeeds, but the operator must run
         `renewal_cli.py install-activate-core-timers`-equivalent for the apply timer
         themselves (the exact command is included in the report) as root.
    Returns a report dict; never raises for an expected failure."""
    report = {"ok": True, "steps": [], "problems": [], "write_approved_created": False,
              "timer_activated": False, "manual_command": None}

    legacy = detect_legacy_bandwidth_automation()
    if legacy["active_write_capable"] and not override_legacy_write_conflict:
        report["ok"] = False
        report["problems"].append(
            f"refusing to enable automatic renewal: a foreign, write-capable bandwidth "
            f"automation job is already active on this host ({legacy['found_active']}). "
            f"This was NOT disabled or modified -- resolve the conflict yourself (retire "
            f"the foreign job, or confirm it is safe to run alongside this one), then "
            f"re-run with override_legacy_write_conflict=True (CLI: "
            f"--override-legacy-write-conflict) if you are certain.")
        report["legacy_bandwidth_automation"] = legacy
        return report

    nodes_path = os.path.join(config_dir, "nodes.json")
    policy_path = os.path.join(config_dir, "policy.json")
    try:
        import nodes as nodes_mod
        nodes_mod.load_nodes(nodes_path)
    except Exception as e:
        report["ok"] = False
        report["problems"].append(f"nodes.json did not load -- refusing to enable writes: {e}")
        return report

    try:
        import policy as pol
        pol.load_policy_config(policy_path)
    except Exception as e:
        report["ok"] = False
        report["problems"].append(f"policy.json did not load -- refusing to enable writes: {e}")
        return report

    marker_path = os.path.join(config_dir, WRITE_APPROVAL_MARKER_NAME)
    if not os.path.exists(marker_path):
        Path(marker_path).write_text(
            "Created by enable_auto_renewal() -- production write capability is "
            "operator-authorized.\n")
        report["write_approved_created"] = True
    report["steps"].append(f"WRITE_APPROVED present at {marker_path}")

    import json
    data = json.loads(Path(policy_path).read_text())
    data["global_defaults"]["auto_renewal_enabled"] = True
    tmp = policy_path + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, policy_path)
    report["steps"].append("policy.json global_defaults.auto_renewal_enabled set to true")

    import service_activation as sa
    if activate_timer:
        if os.geteuid() == 0:
            activation = sa.install_and_enable_units(systemd_src_dir, [sa.WRITE_CAPABLE_UNIT])
            if activation["ok"]:
                report["timer_activated"] = True
                report["steps"].append("virt-renewal-apply.timer installed and enabled")
            else:
                report["ok"] = False
                report["problems"].extend(activation["problems"])
        else:
            service, timer = sa.WRITE_CAPABLE_UNIT
            report["manual_command"] = (
                f"sudo cp {os.path.join(systemd_src_dir, service)} "
                f"{os.path.join(systemd_src_dir, timer)} /etc/systemd/system/ && "
                f"sudo systemctl daemon-reload && sudo systemctl enable --now {timer}")
            report["problems"].append(
                "not running as root -- the apply timer was NOT installed/enabled; "
                f"run this yourself: {report['manual_command']}")

    report["legacy_bandwidth_automation"] = legacy
    if legacy["active_write_capable"]:
        report["steps"].append(
            f"NOTE: proceeded despite a detected foreign write-capable bandwidth job "
            f"({legacy['found_active']}) per override_legacy_write_conflict=True -- that "
            f"job was NOT touched; both are now capable of writing to Virtualizor "
            f"independently.")
    return report


def disable_auto_renewal(config_dir: str, systemd_src_dir: str) -> dict:
    """The single, real implementation behind BOTH `renewal_cli.cmd_disable_auto_renewal`
    and `menu.action_toggle_auto_renewal(False)`. Documented policy (unlike the pre-v1.10.3
    behavior, where removing WRITE_APPROVED was an OPTIONAL extra confirmation): disabling
    automatic renewal ALWAYS revokes write authorization -- there is no supported
    in-between state where auto_renewal_enabled=false but WRITE_APPROVED still exists,
    because that combination is exactly what let the apply timer fire in a 'blocked by
    design' state that looked like a systemd failure on `ir2`. Monitoring/inventory/
    hostnet/readiness-notifier timers and all historical ledger data are explicitly NOT
    touched by this function -- only the write path is revoked."""
    report = {"ok": True, "steps": [], "problems": [], "write_approved_removed": False,
              "timer_stopped": False}

    policy_path = os.path.join(config_dir, "policy.json")
    import json
    try:
        data = json.loads(Path(policy_path).read_text())
        data["global_defaults"]["auto_renewal_enabled"] = False
        tmp = policy_path + ".tmp"
        Path(tmp).write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, policy_path)
        report["steps"].append("policy.json global_defaults.auto_renewal_enabled set to false")
    except Exception as e:
        report["ok"] = False
        report["problems"].append(f"could not update policy.json: {e}")

    marker_path = os.path.join(config_dir, WRITE_APPROVAL_MARKER_NAME)
    if os.path.exists(marker_path):
        os.remove(marker_path)
        report["write_approved_removed"] = True
        report["steps"].append(f"{WRITE_APPROVAL_MARKER_NAME} removed -- writes structurally blocked again")
    else:
        report["steps"].append(f"{WRITE_APPROVAL_MARKER_NAME} was already absent")

    import service_activation as sa
    if os.geteuid() == 0:
        deactivation = sa.disable_and_remove_units([sa.WRITE_CAPABLE_UNIT])
        if deactivation["ok"]:
            report["timer_stopped"] = True
            report["steps"].append("virt-renewal-apply.timer stopped, disabled, and removed")
        else:
            report["ok"] = False
            report["problems"].extend(deactivation["problems"])
    else:
        report["problems"].append(
            "not running as root -- could not confirm/stop any real virt-renewal-apply.timer "
            "that may be installed; if one is, run: sudo systemctl disable --now "
            "virt-renewal-apply.timer")

    return report


def health_check(config_dir: str, state_dir: str, check_systemd: bool = False) -> dict:
    """Consolidated, read-only report of whether the install looks healthy after a change --
    never restarts or enables anything itself.

    `check_systemd` defaults to False HERE because this exact function is also
    `updater.apply_update`'s default post-update rollback gate (see its `health_check_fn`
    parameter) -- an update must never be rolled back just because an install that predates
    v1.10.3's automatic timer activation (e.g. a real pre-v1.10.3 `ir1`/`ir2`-shaped install)
    never had its core timers enabled in the first place; that is an orthogonal, pre-existing
    condition the updater has no business judging. Callers that DO want the real systemd
    check -- `cmd_install_health_check`/the menu's install-health-check screen -- pass
    `check_systemd=True` explicitly. When True, also reports the real, current
    enabled/active/failed state of every one of this project's own timers via
    `service_activation.verify_units_status` (still pure read-only: `systemctl
    is-enabled`/`is-active`/`is-failed`, nothing that changes state) -- added in v1.10.3 so a
    'the install looks fine' report can no longer be true while a core timer is silently
    disabled or a service is stuck failed, the exact gap that made the `ir2` installation's
    problems invisible until an operator went looking for them by hand."""
    report = {"ok": True, "problems": []}

    try:
        import nodes as nodes_mod
        nodes_mod.load_nodes(os.path.join(config_dir, "nodes.json"))
    except Exception as e:
        report["ok"] = False
        report["problems"].append(f"nodes.json did not load: {e}")

    try:
        import policy as pol
        pol.load_policy_config(os.path.join(config_dir, "policy.json"))
    except Exception as e:
        report["ok"] = False
        report["problems"].append(f"policy.json did not load: {e}")

    for optional, settings_loader in (
        ("web", lambda: __import__("web_config").load_web_settings(config_dir)),
        ("telegram", lambda: __import__("telegram_config").load_telegram_settings(config_dir)),
        ("agent", lambda: __import__("agent_config").load_agent_settings(config_dir)),
    ):
        try:
            settings_loader()
        except Exception as e:
            report["ok"] = False
            report["problems"].append(f"{optional} settings did not load: {e}")

    # Purely informational, never affects `ok` -- the presence of a foreign bandwidth
    # automation job is not itself unhealthy (see detect_legacy_bandwidth_automation's
    # docstring); it only matters at the moment enable_auto_renewal() is actually called,
    # where it IS enforced. Surfaced here so an operator checking general health has
    # visibility into it without needing to know to look.
    report["legacy_bandwidth_automation"] = detect_legacy_bandwidth_automation()

    if check_systemd and shutil.which("systemctl"):
        import service_activation as sa
        write_approved = os.path.exists(os.path.join(config_dir, "WRITE_APPROVED"))
        units_to_check = list(sa.CORE_READONLY_UNITS)
        if write_approved:
            units_to_check.append(sa.WRITE_CAPABLE_UNIT)
        systemd_status = sa.verify_units_status(units_to_check)
        report["systemd"] = systemd_status
        for timer, status in systemd_status.items():
            is_core = any(timer == t for _s, t in sa.CORE_READONLY_UNITS)
            if is_core and status["is_enabled"] != "enabled":
                report["ok"] = False
                report["problems"].append(
                    f"{timer} is not enabled (currently: {status['is_enabled']!r}) -- "
                    f"it should be, on any install that completed activation")
            if status["service_is_failed"] == "failed":
                report["ok"] = False
                report["problems"].append(
                    f"{status['service']} is in a FAILED state -- check "
                    f"'journalctl -u {status['service']}' for the real cause")

    return report
