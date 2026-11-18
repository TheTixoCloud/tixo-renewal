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


def health_check(config_dir: str, state_dir: str) -> dict:
    """Consolidated, read-only report of whether the install looks healthy after a change --
    never restarts or enables anything itself."""
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

    return report
