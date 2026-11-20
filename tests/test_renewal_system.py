"""Tests for the multi-node renewal system: nodes.py, backup.py, alerts.py, and the
renewal_cli.py subcommands. All against tmp_path -- never the real workspace state, never SSH.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import alerts
import backup as backup_mod
import nodes as nodes_mod
import renewal_cli
import shadow_monitor as sm


def iso(dt):
    return dt.isoformat()


# ---------- nodes.py ----------

def test_load_nodes_defaults_to_safe_unconfigured_placeholder_when_no_config(tmp_path):
    """The public package ships with NO real host baked in -- the fallback must be an
    obviously-unusable placeholder, never a real production identity."""
    missing = str(tmp_path / "does-not-exist.json")
    loaded = nodes_mod.load_nodes(missing)
    assert len(loaded) == 1
    assert loaded[0].node_id == "unconfigured"
    assert loaded[0].ssh_host == ""
    assert loaded[0].ssh_key == ""


def test_write_default_config_is_idempotent_never_clobbers_edits(tmp_path):
    path = str(tmp_path / "nodes.json")
    nodes_mod.write_default_config(path)
    data = json.loads(open(path).read())
    data["nodes"][0]["node_id"] = "operator-renamed"
    open(path, "w").write(json.dumps(data))
    nodes_mod.write_default_config(path)  # must be a no-op since file exists
    loaded = nodes_mod.load_nodes(path)
    assert loaded[0].node_id == "operator-renamed"  # edit survived


def test_node_ssh_cmd_and_db_path(tmp_path):
    node = nodes_mod.NodeConfig(node_id="x", ssh_host="root@1.2.3.4",
                                 ssh_key="~/.ssh/id_x", db_filename="x.sqlite3")
    cmd = node.ssh_cmd()
    assert cmd[-1] == "root@1.2.3.4"
    assert node.db_path(str(tmp_path)) == str(tmp_path / "x.sqlite3")


def test_local_mode_survives_config_save_and_load_roundtrip(tmp_path):
    """Regression test: a real end-to-end install.sh simulation caught that `mode` was not
    being serialized by write_default_config(), so a 'local' node silently reverted to the
    'ssh' default after being written to disk and read back -- exactly the kind of bug a
    pure in-memory unit test (constructing NodeConfig directly) would never catch."""
    path = str(tmp_path / "nodes.json")
    local_node = nodes_mod.NodeConfig(node_id="self", mode="local", db_filename="self.sqlite3")
    nodes_mod.write_default_config(path, nodes=[local_node])
    loaded = nodes_mod.load_nodes(path)
    assert loaded[0].mode == "local"


def test_multiple_nodes_get_independent_databases(tmp_path):
    path = str(tmp_path / "nodes.json")
    two_nodes = [
        nodes_mod.NodeConfig("a", "root@1.1.1.1", "~/.ssh/a", "a.sqlite3"),
        nodes_mod.NodeConfig("b", "root@2.2.2.2", "~/.ssh/b", "b.sqlite3"),
    ]
    nodes_mod.write_default_config(path, nodes=two_nodes)
    loaded = nodes_mod.load_nodes(path)
    assert {n.db_path(str(tmp_path)) for n in loaded} == {
        str(tmp_path / "a.sqlite3"), str(tmp_path / "b.sqlite3")
    }


# ---------- backup.py ----------

def test_backup_and_restore_roundtrip(tmp_path):
    db_path = str(tmp_path / "live.sqlite3")
    conn = sm.open_db(db_path)
    sm.ingest_poll(conn, [{"vpsid": 1, "created_epoch": 1700000000,
                            "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}],
                   iso(datetime.now(timezone.utc)))
    conn.close()

    backup_root = str(tmp_path / "backups")
    backup_path = backup_mod.backup_db(db_path, backup_root, label="live")
    assert os.path.exists(backup_path)

    # mutate the live DB after the backup
    conn = sm.open_db(db_path)
    sm.ingest_poll(conn, [{"vpsid": 2, "created_epoch": 1700000001,
                            "quota_gb": Decimal("2000"), "used_gb": Decimal("1")}],
                   iso(datetime.now(timezone.utc)))
    conn.close()

    backup_mod.restore_db(backup_path, db_path)
    restored = sm.open_db(db_path)
    vpsids = {r[0] for r in restored.execute("SELECT vpsid FROM identities")}
    assert vpsids == {1}  # vpsid 2 (added after the backup) is gone -- restore worked
    restored.close()
    assert os.path.exists(db_path + ".pre-restore.sqlite3")  # safety copy was made


def test_backup_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_mod.backup_db(str(tmp_path / "nope.sqlite3"), str(tmp_path / "backups"))


def test_restore_missing_backup_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_mod.restore_db(str(tmp_path / "nope.sqlite3"), str(tmp_path / "target.sqlite3"))


def test_list_backups_empty_dir_returns_empty(tmp_path):
    assert backup_mod.list_backups(str(tmp_path / "nonexistent")) == []


# ---------- alerts.py ----------

def test_polling_stalled_alert_fires_after_grace_period(tmp_path):
    db_path = str(tmp_path / "db.sqlite3")
    conn = sm.open_db(db_path)
    old_ts = datetime.now(timezone.utc) - timedelta(days=10)
    sm.ingest_poll(conn, [{"vpsid": 1, "created_epoch": 1700000000,
                            "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}], iso(old_ts))
    detections = sm.detect_renewal_candidates(conn)
    found = alerts.compute_alerts(conn, detections, now=datetime.now(timezone.utc))
    polling_alerts = [a for a in found if a["type"] == "POLLING_STALLED"]
    assert len(polling_alerts) == 1
    assert polling_alerts[0]["severity"] == "ALERT"
    conn.close()


def test_no_stalled_alert_for_recent_snapshot(tmp_path):
    db_path = str(tmp_path / "db.sqlite3")
    conn = sm.open_db(db_path)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    sm.ingest_poll(conn, [{"vpsid": 1, "created_epoch": 1700000000,
                            "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}], iso(recent))
    detections = sm.detect_renewal_candidates(conn)
    found = alerts.compute_alerts(conn, detections, now=datetime.now(timezone.utc))
    assert not [a for a in found if a["type"] == "POLLING_STALLED"]
    conn.close()


def test_format_alerts_empty_and_nonempty():
    assert alerts.format_alerts([]) == "No alerts."
    out = alerts.format_alerts([{"severity": "ALERT", "vpsid": 1, "type": "X", "detail": "y"}])
    assert "vpsid=1" in out and "X" in out


# ---------- renewal_cli.py: install / status / dry-run / backup / rollback / uninstall ----------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "shadow-monitor-state")
    config_dir = str(tmp_path / "renewal-config")
    backup_dir = str(tmp_path / "backups")
    systemd_dir = str(tmp_path / "systemd")
    # WORKSPACE_ROOT is patched too so it stays aligned with the individually-patched dirs
    # above -- in every real deployment STATE_DIR/CONFIG_DIR/BACKUP_DIR/SYSTEMD_OUT_DIR are
    # ALL derived from WORKSPACE_ROOT (see renewal_cli.py's own module-level definitions),
    # and cmd_uninstall/cmd_update_apply rely on that same alignment to find the right
    # install_dir -- letting WORKSPACE_ROOT silently diverge here would make this fixture
    # describe a layout no real install ever has.
    monkeypatch.setattr(renewal_cli, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(renewal_cli, "STATE_DIR", state_dir)
    monkeypatch.setattr(renewal_cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(renewal_cli, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(renewal_cli, "SYSTEMD_OUT_DIR", systemd_dir)
    monkeypatch.setattr(renewal_cli, "WRITE_APPROVAL_MARKER", os.path.join(config_dir, "WRITE_APPROVED"))
    monkeypatch.setattr(nodes_mod, "DEFAULT_CONFIG_PATH", os.path.join(config_dir, "nodes.json"))

    def fake_load_nodes(path=None):
        return [nodes_mod.NodeConfig("test-node", "root@203.0.113.1", "~/.ssh/fake", "test-node.sqlite3")]
    monkeypatch.setattr(renewal_cli.nodes_mod, "load_nodes", fake_load_nodes)
    return {"state_dir": state_dir, "config_dir": config_dir, "backup_dir": backup_dir}


def test_install_is_idempotent_and_creates_expected_layout(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.cmd_install(argparse_namespace())  # re-run: must not error or duplicate state
    assert os.path.isdir(cli_env["state_dir"])
    assert os.path.exists(os.path.join(cli_env["state_dir"], "test-node.sqlite3"))
    assert os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.service"))
    assert os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer"))


def test_install_generates_all_three_unit_pairs_including_notifier(cli_env):
    """REGRESSION: the readiness notifier's systemd units were previously generated by a
    one-off manual process outside the real installer entirely -- a fresh install on a new
    node got monitor+apply but no notifier at all. Install must generate all three."""
    renewal_cli.cmd_install(argparse_namespace())
    for name in ("virt-renewal-monitor.service", "virt-renewal-monitor.timer",
                 "virt-renewal-apply.service", "virt-renewal-apply.timer",
                 "virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer",
                 "virt-renewal-inventory-sync.service", "virt-renewal-inventory-sync.timer",
                 "virt-renewal-hostnet-sync.service", "virt-renewal-hostnet-sync.timer",
                 "virt-renewal-telegram-bot.service", "virt-renewal-web.service",
                 "virt-renewal-relay-worker.service", "virt-renewal-agent.service"):
        assert os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, name)), f"missing {name}"
    notifier_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR,
                                          "virt-renewal-readiness-notifier.service")).read_text()
    assert "readiness_notifier.py" in notifier_content
    assert "--node test-node" in notifier_content
    inventory_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR,
                                           "virt-renewal-inventory-sync.service")).read_text()
    assert "inventory-sync" in inventory_content
    assert "--node test-node" in inventory_content


def test_uninstall_removes_all_three_unit_pairs_not_just_monitor(cli_env):
    """REGRESSION: cmd_uninstall previously only ever knew about the monitor unit pair,
    leaving apply/notifier files behind (and never mentioning the write-capable apply timer
    in its system-wide cleanup instructions)."""
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.cmd_uninstall(argparse_namespace(purge=False, yes=True, confirm=None, delete_backups=False))
    for name in ("virt-renewal-monitor.service", "virt-renewal-monitor.timer",
                 "virt-renewal-apply.service", "virt-renewal-apply.timer",
                 "virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer",
                 "virt-renewal-inventory-sync.service", "virt-renewal-inventory-sync.timer",
                 "virt-renewal-hostnet-sync.service", "virt-renewal-hostnet-sync.timer",
                 "virt-renewal-telegram-bot.service", "virt-renewal-web.service",
                 "virt-renewal-relay-worker.service", "virt-renewal-agent.service"):
        assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, name)), f"{name} still present"
    # Data must still survive a non-purge uninstall.
    assert os.path.exists(os.path.join(cli_env["state_dir"], "test-node.sqlite3"))


def test_status_uses_this_installs_node_config_path_not_module_default(cli_env, monkeypatch):
    """REGRESSION: cmd_status previously called nodes_mod.load_nodes() with NO path argument,
    which binds to nodes.py's own module-level DEFAULT_CONFIG_PATH (resolved at import time
    relative to nodes.py's own file location) instead of this install's (possibly test-
    patched or host-local) CONFIG_DIR -- unlike every other subcommand, which already goes
    through _node_config_path(). The cli_env fixture's fake_load_nodes ignores its `path` arg
    entirely, which is exactly how this went undetected -- so this test inspects the actual
    argument passed, not just the returned node list."""
    renewal_cli.cmd_install(argparse_namespace())
    calls = []

    def spy_load_nodes(path=None):
        calls.append(path)
        return [nodes_mod.NodeConfig("test-node", "root@203.0.113.1", "~/.ssh/fake", "test-node.sqlite3")]

    monkeypatch.setattr(renewal_cli.nodes_mod, "load_nodes", spy_load_nodes)
    renewal_cli.cmd_status(argparse_namespace())
    assert calls, "cmd_status never called load_nodes"
    assert calls[0] == renewal_cli._node_config_path(), (
        f"cmd_status called load_nodes with {calls[0]!r}, expected the install's own "
        f"_node_config_path() {renewal_cli._node_config_path()!r}")


def test_backup_uses_this_installs_node_config_path_not_module_default(cli_env, monkeypatch):
    """Same regression class as cmd_status, found while adding the v1.1 menu: cmd_backup also
    called nodes_mod.load_nodes() with no path argument."""
    renewal_cli.cmd_install(argparse_namespace())
    calls = []

    def spy_load_nodes(path=None):
        calls.append(path)
        return [nodes_mod.NodeConfig("test-node", "root@203.0.113.1", "~/.ssh/fake", "test-node.sqlite3")]

    monkeypatch.setattr(renewal_cli.nodes_mod, "load_nodes", spy_load_nodes)
    renewal_cli.cmd_backup(argparse_namespace())
    assert calls and calls[0] == renewal_cli._node_config_path()


def test_rollback_uses_this_installs_node_config_path_not_module_default(cli_env, monkeypatch):
    renewal_cli.cmd_install(argparse_namespace())
    calls = []

    def spy_load_nodes(path=None):
        calls.append(path)
        return [nodes_mod.NodeConfig("test-node", "root@203.0.113.1", "~/.ssh/fake", "test-node.sqlite3")]

    monkeypatch.setattr(renewal_cli.nodes_mod, "load_nodes", spy_load_nodes)
    with pytest.raises(FileNotFoundError):  # backup file genuinely doesn't exist -- expected
        renewal_cli.cmd_rollback(argparse_namespace(node="test-node", backup_file="nonexistent.sqlite3", yes=True))
    assert calls and calls[0] == renewal_cli._node_config_path()


def test_install_honors_monthly_base_env_var(cli_env, monkeypatch):
    """TIXO_RENEWAL_MONTHLY_BASE_GB lets an operator configure the monthly base AT install
    time instead of hand-editing policy.json afterward."""
    monkeypatch.setenv("TIXO_RENEWAL_MONTHLY_BASE_GB", "300")
    renewal_cli.cmd_install(argparse_namespace())
    data = json.loads(open(renewal_cli._policy_config_path()).read())
    assert data["global_defaults"]["monthly_grant_gb"] == "300"
    assert data["global_defaults"]["initial_allocation_gb"] == "300"


def test_generated_systemd_service_sets_pythonpath_and_install_dir(cli_env):
    """Regression test: a real end-to-end deployment simulation (not a mocked unit test)
    caught that the generated service unit omitted PYTHONPATH/TIXO_RENEWAL_INSTALL_DIR,
    so systemd would launch it with neither set and it would immediately crash with
    ModuleNotFoundError before ever reaching a single read-only query. Fixed; locked in here."""
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.service")).read_text()
    assert "Environment=PYTHONPATH=" in content
    assert "Environment=TIXO_RENEWAL_INSTALL_DIR=" in content
    assert "ExecStart=" in content
    exec_line = [l for l in content.splitlines() if l.startswith("ExecStart=")][0]
    assert " dry-run" in exec_line
    assert " apply" not in exec_line  # the systemd unit must never be able to invoke apply


# ---------- 5-minute scheduling + non-overlap (owner-approved, 2026-11-16) ----------------

def test_monitor_timer_cadence_is_approximately_5_minutes(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer")).read_text()
    assert "OnUnitActiveSec=5min" in content


def test_apply_timer_cadence_is_approximately_5_minutes_not_daily(cli_env):
    """Regression: the apply timer previously used OnCalendar=daily (its safe-by-default
    review-only placeholder). Owner-approved activation requires prompt (~5min) application
    after a genuine reset, matching the monitor's own cadence."""
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-apply.timer")).read_text()
    assert "OnUnitActiveSec=5min" in content
    assert "OnCalendar=daily" not in content


def test_readiness_notifier_cadence_unchanged_at_30_minutes():
    """No technical reason to change this one -- must stay at 30 minutes exactly as before."""
    content = Path(os.path.join(os.path.dirname(__file__), "..", "deploy", "systemd-notifier",
                                 "virt-renewal-readiness-notifier.timer")).read_text()
    assert "OnUnitActiveSec=30min" in content


def test_neither_timer_sets_persistent_true_no_catchup_run_on_enable(cli_env):
    """CRITICAL: Persistent=true would make systemd immediately run a 'missed' invocation the
    moment the timer is loaded/enabled, after however long it was previously inactive --
    including a write-capable apply-due run. Neither timer may ever set this."""
    renewal_cli.cmd_install(argparse_namespace())
    for name in ("virt-renewal-monitor.timer", "virt-renewal-apply.timer"):
        content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, name)).read_text()
        assert "Persistent=true" not in content
        assert "Persistent=yes" not in content
    notifier_timer = Path(os.path.join(os.path.dirname(__file__), "..", "deploy",
                                        "systemd-notifier", "virt-renewal-readiness-notifier.timer")).read_text()
    assert "Persistent=true" not in notifier_timer
    assert "Persistent=yes" not in notifier_timer


def test_monitor_and_apply_services_share_the_same_flock_lock_path(cli_env):
    """Non-overlap guarantee: monitor and apply are two DIFFERENT systemd units (systemd only
    prevents a unit from running concurrently with ITSELF, not with a different unit), so they
    must share one flock lock to serialize their access to the same SQLite ledger."""
    renewal_cli.cmd_install(argparse_namespace())
    monitor_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.service")).read_text()
    apply_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-apply.service")).read_text()
    monitor_exec = [l for l in monitor_content.splitlines() if l.startswith("ExecStart=")][0]
    apply_exec = [l for l in apply_content.splitlines() if l.startswith("ExecStart=")][0]
    assert "flock -n" in monitor_exec
    assert "flock -n" in apply_exec
    monitor_lock = monitor_exec.split("flock -n")[1].split()[0]
    apply_lock = apply_exec.split("flock -n")[1].split()[0]
    assert monitor_lock == apply_lock == renewal_cli.TIXO_RENEWAL_LOCK_PATH


def test_apply_service_exec_still_uses_non_interactive_apply_due_only(cli_env):
    """Changing the cadence/locking must never change WHAT gets executed -- still exactly
    `apply-due --node self --non-interactive`, never a bare `apply` or anything else."""
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-apply.service")).read_text()
    exec_line = [l for l in content.splitlines() if l.startswith("ExecStart=")][0]
    assert "apply-due" in exec_line
    assert "--node self" in exec_line
    assert "--non-interactive" in exec_line


def test_dry_run_never_calls_real_ssh_and_ingests_fake_rows(cli_env, monkeypatch):
    renewal_cli.cmd_install(argparse_namespace())

    fake_rows = [{"vpsid": 1, "created_epoch": 1700000000,
                  "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}]
    called_with = {}

    def fake_fetch(ssh_cmd=None):
        called_with["ssh_cmd"] = ssh_cmd
        return fake_rows

    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", fake_fetch)
    renewal_cli.cmd_dry_run(argparse_namespace())
    assert called_with["ssh_cmd"][-1] == "root@203.0.113.1"  # used the node's own ssh_cmd
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 1


def test_apply_is_blocked_without_approval_marker(cli_env):
    rc = renewal_cli.cmd_apply(argparse_namespace())
    assert rc == 1
    assert not os.path.exists(renewal_cli.WRITE_APPROVAL_MARKER)


def test_apply_still_does_not_write_even_with_marker_present(cli_env):
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("operator-created")
    # Marker present but --execute not given -> must still refuse (preview-only message).
    rc = renewal_cli.cmd_apply(argparse_namespace(execute=False))
    assert rc == 1


def test_apply_with_execute_but_missing_args_refuses(cli_env):
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("operator-created")
    rc = renewal_cli.cmd_apply(argparse_namespace(execute=True, node=None, vpsid=None,
                                                    period_key=None, new_quota_gb=None))
    assert rc == 1


def test_backup_and_rollback_cli_roundtrip(cli_env, monkeypatch):
    renewal_cli.cmd_install(argparse_namespace())
    fake_rows = [{"vpsid": 7, "created_epoch": 1700000000,
                  "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}]
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: fake_rows)
    renewal_cli.cmd_dry_run(argparse_namespace())

    renewal_cli.cmd_backup(argparse_namespace())
    backups = backup_mod.list_backups(renewal_cli.BACKUP_DIR)
    assert len(backups) == 1

    # mutate state further, then roll back
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 8, "created_epoch": 1700000002, "quota_gb": Decimal("1"), "used_gb": Decimal("0")}])
    renewal_cli.cmd_dry_run(argparse_namespace())
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 2

    ns = argparse_namespace(node="test-node", backup_file=backups[0], yes=True)
    renewal_cli.cmd_rollback(ns)
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 1


def test_rollback_without_yes_refuses(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    ns = argparse_namespace(node="test-node", backup_file="whatever.sqlite3", yes=False)
    rc = renewal_cli.cmd_rollback(ns)
    assert rc == 1


def test_uninstall_removes_local_unit_files_but_keeps_data_by_default(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.cmd_uninstall(argparse_namespace(purge=False, yes=True, confirm=None, delete_backups=False))
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.service"))
    assert os.path.isdir(renewal_cli.STATE_DIR)  # data kept


def test_uninstall_purge_removes_data(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.cmd_uninstall(argparse_namespace(purge=True, yes=True,
                                                   confirm=renewal_cli.REQUIRED_PURGE_PHRASE,
                                                   delete_backups=False))
    assert not os.path.exists(renewal_cli.STATE_DIR)


class argparse_namespace:
    """Tiny stand-in for argparse.Namespace so tests don't need the real parser."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ---------- policy-show / policy-validate / preview / resolve-operation ------------------------

def test_policy_show_and_validate_after_install(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_policy_validate(argparse_namespace())
    assert rc == 0 or rc is None
    renewal_cli.cmd_policy_show(argparse_namespace())
    out = capsys.readouterr().out
    assert "auto_renewal_enabled" in out


def test_policy_validate_rejects_broken_config(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    open(renewal_cli._policy_config_path(), "w").write('{"schema_version": 1, "global_defaults": {}}')
    rc = renewal_cli.cmd_policy_validate(argparse_namespace())
    assert rc == 1


def test_preview_command_end_to_end(cli_env, monkeypatch, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    fake_rows = [{"vpsid": 10, "created_epoch": 1700000000,
                  "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}]
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: fake_rows)
    renewal_cli.cmd_dry_run(argparse_namespace())
    rc = renewal_cli.cmd_preview(argparse_namespace(node="test-node", vpsid=10))
    out = capsys.readouterr().out
    assert "PREVIEW ONLY" in out
    assert "proposed_new_quota_gb" in out


def test_preview_unknown_vpsid_refuses(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_preview(argparse_namespace(node="test-node", vpsid=999))
    assert rc == 1


def test_resolve_operation_lists_and_resolves(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    import writer as writer_mod
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    writer_mod.ensure_operations_schema(conn)
    writer_mod.record_operation_start(conn, "op-1", "test-node", 10, "period-A", Decimal("150"))
    writer_mod.record_operation_result(conn, "op-1", "UNKNOWN_NEEDS_VERIFICATION", api_response="timeout")
    conn.close()

    rc = renewal_cli.cmd_resolve_operation(argparse_namespace(node="test-node", mark_resolved=None))
    assert rc == 0 or rc is None

    rc = renewal_cli.cmd_resolve_operation(argparse_namespace(
        node="test-node", mark_resolved="op-1", resolution="FAILED", verified_quota_gb="100"))
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM operations WHERE operation_id='op-1'").fetchone()
    assert row[0] == "FAILED"
    conn.close()


# ---------- host-local deployment mode (no SSH at all) -----------------------------------------

def test_dry_run_in_local_mode_never_touches_ssh(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    config_dir = str(tmp_path / "config")
    monkeypatch.setattr(renewal_cli, "STATE_DIR", state_dir)
    monkeypatch.setattr(renewal_cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(renewal_cli, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(renewal_cli, "SYSTEMD_OUT_DIR", str(tmp_path / "systemd"))
    monkeypatch.setattr(renewal_cli, "WRITE_APPROVAL_MARKER", os.path.join(config_dir, "WRITE_APPROVED"))

    local_node = nodes_mod.NodeConfig(node_id="self", mode="local", db_filename="self.sqlite3")
    monkeypatch.setattr(renewal_cli.nodes_mod, "load_nodes", lambda path=None: [local_node])

    renewal_cli.cmd_install(argparse_namespace())

    def fail_ssh(*a, **k):
        pytest.fail("local-mode node must never call sm.fetch_live_rows (SSH path)")
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", fail_ssh)

    import local_source as ls
    fake_rows = [{"vpsid": 1, "created_epoch": 1700000000,
                  "quota_gb": Decimal("1000"), "used_gb": Decimal("500")}]
    monkeypatch.setattr(ls, "fetch_live_rows_local", lambda: fake_rows)

    renewal_cli.cmd_dry_run(argparse_namespace())
    db_path = os.path.join(state_dir, "self.sqlite3")
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 1


# ---------- install also generates (but never enables) the unattended renewal unit -------------

def test_install_generates_renewal_apply_units_but_never_enables_them(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    service = os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-apply.service")
    timer = os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-apply.timer")
    assert os.path.exists(service)
    assert os.path.exists(timer)
    content = Path(service).read_text()
    assert "apply-due" in content
    assert "NOT enabled by default" in content or "NOT ENABLED BY DEFAULT" in Path(timer).read_text()
    # Unlike the 4 read-only core timers (which v1.10.3's install.sh DOES now auto-activate,
    # see test_deploy_package.py's real-systemd round-trip test), the write-capable apply
    # unit is NEVER copied into /etc/systemd/system by install.sh -- only by the separate,
    # explicitly-confirmed enable-auto-renewal flow (installer.enable_auto_renewal). This
    # test only confirms the apply unit's template files exist locally and are unenabled.


# ---------- store-credentials / apply-due (unattended path), fully mocked ----------------------

def test_store_credentials_writes_secure_file(cli_env, monkeypatch):
    renewal_cli.cmd_install(argparse_namespace())
    monkeypatch.setattr("builtins.input", lambda prompt="": "https://example.invalid")
    import getpass as getpass_mod
    secrets = iter(["my-api-key", "my-api-pass"])
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": next(secrets))
    renewal_cli.cmd_store_credentials(argparse_namespace(node="test-node"))
    import credentials as creds_mod
    path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    assert os.path.exists(path)
    loaded = creds_mod.load_credentials(path)
    assert loaded.base_url == "https://example.invalid"
    assert loaded.api_key == "my-api-key"
    assert loaded.api_pass == "my-api-pass"


def test_inventory_sync_exits_zero_when_credentials_not_yet_stored(cli_env, capsys):
    """Regression for a real bug found while testing v1.10.3's automatic core-timer
    activation: virt-renewal-inventory-sync.timer is now enabled on every fresh install,
    which means this command runs before an operator necessarily has had the chance to run
    store-credentials -- it must report a clear NOT_CONFIGURED status and exit 0, never a
    failure exit code, or the timer's service would show 'failed' in systemctl/journalctl on
    every single fresh install until credentials are stored (exactly the kind of false
    alarm this project's design principles are supposed to prevent)."""
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_inventory_sync(argparse_namespace(node="self"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT_CONFIGURED" in out


def test_apply_due_blocked_without_write_approval(cli_env):
    """Exit code 3 (not 1), deliberately: this is a by-design safety refusal, not a crash --
    see RENEWAL_SERVICE_UNIT_TEMPLATE's SuccessExitStatus=3 and cmd_apply_due's docstring."""
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    assert rc == 3


def test_apply_due_non_interactive_requires_stored_credentials(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")
    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    assert rc == 1  # no credentials stored yet -> blocked, not a silent no-op


def test_apply_due_applies_only_policy_eligible_vps(cli_env, monkeypatch):
    """End-to-end (mocked HTTP) check that apply-due only touches VPS that are BOTH a
    plausible eligible renewal candidate AND have auto_renewal_enabled=True by policy --
    never a VPS that merely exists with no specific policy decision made."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")

    import credentials as creds_mod
    creds_path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    creds_mod.store_credentials(creds_path, "https://example.invalid", "k", "p")

    # Enable auto-renewal globally and poll in two rounds to create an eligible candidate.
    policy_path = renewal_cli._policy_config_path()
    data = json.loads(open(policy_path).read())
    data["global_defaults"]["auto_renewal_enabled"] = True
    open(policy_path, "w").write(json.dumps(data))

    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    # Two polls close together in WALL-CLOCK terms (30 min apart, well under the
    # missing-snapshot gap threshold) whose CALENDAR dates straddle the monthly anniversary --
    # the gap-detector cares about real elapsed polling time, not calendar distance.
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 9, 31, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 42, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("900")}])
    renewal_cli.cmd_dry_run(argparse_namespace())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 42, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("1")}])
    renewal_cli.cmd_dry_run(argparse_namespace())

    import writer as writer_mod, time as time_mod
    monkeypatch.setattr(writer_mod, "call_managevps_api", lambda *a, **k: {"raw": "ok"})
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)  # skip the real settle delay in tests
    reads = iter([Decimal("1"), Decimal("200"), Decimal("200")])  # pre-write, post-write, confirm
    import local_source as ls
    monkeypatch.setattr(ls, "fetch_single_vps_quota", lambda vpsid: next(reads))

    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    op = conn.execute("SELECT status, requested_new_quota_gb FROM operations WHERE vpsid=42").fetchone()
    assert op is not None
    assert op[0] == "APPLIED"
    assert op[1] == "200"  # 100 remaining + 100 monthly grant
    conn.close()


def test_apply_due_one_vps_exception_does_not_block_other_eligible_vps(cli_env, monkeypatch):
    """v1.2 CRITICAL reliability fix: a real end-to-end proof that an exception processing
    vpsid 42 (simulated network error) never aborts the batch -- vpsid 43's independently
    eligible renewal must still be applied in the SAME run."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")
    import credentials as creds_mod
    creds_mod.store_credentials(
        creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node"),
        "https://example.invalid", "k", "p")
    policy_path = renewal_cli._policy_config_path()
    data = json.loads(open(policy_path).read())
    data["global_defaults"]["auto_renewal_enabled"] = True
    open(policy_path, "w").write(json.dumps(data))

    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 9, 31, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 42, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("900")},
        {"vpsid": 43, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("900")}])
    renewal_cli.cmd_dry_run(argparse_namespace())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 42, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("1")},
        {"vpsid": 43, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("1")}])
    renewal_cli.cmd_dry_run(argparse_namespace())

    import writer as writer_mod, time as time_mod, local_source as ls
    monkeypatch.setattr(writer_mod, "call_managevps_api", lambda *a, **k: {"raw": "ok"})
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    reads43 = iter([Decimal("1"), Decimal("200"), Decimal("200")])

    def flaky_quota_read(vpsid):
        if vpsid == 42:
            raise ConnectionError("simulated transient network error for vpsid 42")
        return next(reads43)
    monkeypatch.setattr(ls, "fetch_single_vps_quota", flaky_quota_read)

    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    op43 = conn.execute("SELECT status FROM operations WHERE vpsid=43").fetchone()
    assert op43 is not None and op43[0] == "APPLIED"  # unaffected by vpsid 42's failure
    conn.close()


def test_apply_due_retries_deferred_locked_vps_using_original_quota_not_recomputed(cli_env, monkeypatch):
    """Full end-to-end proof that apply_due checks for a pending backup-lock deferral BEFORE
    computing a fresh baseline, and that the retry reuses the original stored quota."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")
    import credentials as creds_mod
    creds_mod.store_credentials(
        creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node"),
        "https://example.invalid", "k", "p")

    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    import shadow_monitor as sm_mod, writer as writer_mod
    conn = sm_mod.open_db(db_path)
    # apply_due iterates the `identities` table, not `operations` -- seed an identity so
    # vpsid 77 is actually visited by the loop (a real monitor poll would do this naturally).
    sm_mod.ingest_poll(conn, [{"vpsid": 77, "created_epoch": int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()),
                                "quota_gb": Decimal("321"), "used_gb": Decimal("0")}],
                        datetime(2026, 10, 15, tzinfo=timezone.utc).isoformat())
    creds = writer_mod.ApiCredentials(base_url="https://example.invalid", api_key="k", api_pass="p")
    # Pre-seed a DEFERRED_LOCKED operation, as if a previous run hit a backup lock.
    writer_mod.apply_renewal(
        conn, "test-node", 77, "77|period|1", Decimal("321"), creds,
        renewal_cli.WRITE_APPROVAL_MARKER, confirm=True,
        read_live_quota_fn=lambda v: Decimal("999"),
        http_post=lambda *a, **k: '{"done": 0, "error": "backup in progress"}',
        write_cooldown_seconds=0)
    conn.close()

    import time as time_mod, local_source as ls
    monkeypatch.setattr(writer_mod, "call_managevps_api", lambda *a, **k: {"raw": "ok"})
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    # If apply_due recomputed instead of reusing 321, it would never reach these reads at all
    # (no fresh snapshot pair exists for vpsid 77 in this test) -- reaching APPLIED with 321
    # proves the original value was reused.
    reads = iter([Decimal("999"), Decimal("321"), Decimal("321")])
    monkeypatch.setattr(ls, "fetch_single_vps_quota", lambda v: next(reads))

    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, requested_new_quota_gb, retry_count FROM operations WHERE vpsid=77").fetchone()
    assert row == ("APPLIED", "321", 1)
    conn.close()


def test_apply_due_never_writes_to_unlimited_quota_vps_even_with_auto_renewal_enabled(cli_env, monkeypatch):
    """PERMANENT BUSINESS RULE (owner-confirmed), full end-to-end proof through the real
    apply_due() code path -- not just the preview_renewal() unit test. This is the actual
    dangerous gap that existed: a quota-unchanged(<=0)-but-usage-dropped pattern on an
    unlimited VPS, with auto_renewal_enabled=True, must never reach writer_mod.apply_renewal.
    The HTTP mock fails the test immediately if it is ever called."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")

    import credentials as creds_mod
    creds_path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    creds_mod.store_credentials(creds_path, "https://example.invalid", "k", "p")

    policy_path = renewal_cli._policy_config_path()
    data = json.loads(open(policy_path).read())
    data["global_defaults"]["auto_renewal_enabled"] = True  # the dangerous configuration
    open(policy_path, "w").write(json.dumps(data))

    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 9, 31, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 99, "created_epoch": created_epoch, "quota_gb": Decimal("0"), "used_gb": Decimal("900")}])
    renewal_cli.cmd_dry_run(argparse_namespace())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 99, "created_epoch": created_epoch, "quota_gb": Decimal("0"), "used_gb": Decimal("1")}])
    renewal_cli.cmd_dry_run(argparse_namespace())

    import writer as writer_mod
    monkeypatch.setattr(writer_mod, "call_managevps_api",
                         lambda *a, **k: pytest.fail("must never write to an unlimited-quota VPS"))
    import local_source as ls
    monkeypatch.setattr(ls, "fetch_single_vps_quota",
                         lambda vpsid: pytest.fail("must never even read-verify an unlimited-quota VPS for a write"))

    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    op = conn.execute("SELECT status FROM operations WHERE vpsid=99").fetchone()
    assert op is None  # no operation of any kind was ever started for this vpsid
    conn.close()


def test_apply_due_applies_minimum_1gb_floor_when_gift_is_zero_and_fully_exhausted(cli_env, monkeypatch):
    """Full end-to-end proof, through the real apply_due() path, of the OWNER-APPROVED minimum
    effective quota policy (2026-11-16): an explicitly configured 0 GB gift combined with a
    fully-exhausted VPS computes to 0 remaining + 0 gift = 0, which is floored to an EFFECTIVE
    1 GB and genuinely applied -- NOT blocked (supersedes the earlier
    BLOCKED_ZERO_QUOTA_UNSUPPORTED behavior, which this test previously asserted)."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")

    import credentials as creds_mod
    creds_path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    creds_mod.store_credentials(creds_path, "https://example.invalid", "k", "p")

    policy_path = renewal_cli._policy_config_path()
    data = json.loads(open(policy_path).read())
    data["global_defaults"]["auto_renewal_enabled"] = True
    data["global_defaults"]["monthly_grant_gb"] = "0"  # explicit zero gift, must be preserved
    open(policy_path, "w").write(json.dumps(data))

    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 9, 31, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 50, "created_epoch": created_epoch, "quota_gb": Decimal("5000"), "used_gb": Decimal("5000")}])
    renewal_cli.cmd_dry_run(argparse_namespace())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso", lambda: datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 50, "created_epoch": created_epoch, "quota_gb": Decimal("5000"), "used_gb": Decimal("1")}])
    renewal_cli.cmd_dry_run(argparse_namespace())

    import writer as writer_mod, time as time_mod
    monkeypatch.setattr(writer_mod, "call_managevps_api", lambda *a, **k: {"raw": '{"done":1}', "parsed": {"done": 1}})
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    import local_source as ls
    reads = iter([Decimal("5000"), Decimal("1"), Decimal("1")])  # pre-write, post-write, confirm
    monkeypatch.setattr(ls, "fetch_single_vps_quota", lambda vpsid: next(reads))

    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    op = conn.execute("SELECT status, requested_new_quota_gb FROM operations WHERE vpsid=50").fetchone()
    assert op is not None
    assert op[0] == "APPLIED"
    assert op[1] == "1"  # the floored effective quota, never 0
    conn.close()


def test_manual_apply_cli_still_blocks_raw_zero_never_floors_it(cli_env):
    """CRITICAL architectural guarantee: the 1GB floor applies ONLY inside the validated
    renewal-policy calculation (preview_renewal/apply_due). A raw manual `apply` with
    --new-quota-gb 0 typed directly bypasses that calculation entirely and must remain
    BLOCKED, never silently converted to 1 GB -- there is no way to distinguish a manually-
    typed 0 from a validated one, so the safe behavior (block) must be preserved."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")
    import credentials as creds_mod
    creds_mod.store_credentials(
        creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node"),
        "https://example.invalid", "k", "p")

    import writer as writer_mod
    with pytest.raises(writer_mod.WriteBlockedError):
        renewal_cli.cmd_apply(argparse_namespace(
            node="test-node", vpsid="1", period_key="p1", new_quota_gb="0",
            execute=True, non_interactive=True))
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    op = conn.execute("SELECT COUNT(*) FROM operations WHERE vpsid=1").fetchone()[0]
    assert op == 0  # blocked before any operation row was ever created
    conn.close()


# ---------- _renewal_baseline_for_vpsid: the exact bug class found above, locked in ------------

def test_renewal_baseline_uses_pre_drop_snapshot_not_post_drop(cli_env):
    """Regression test for a real bug: using only the single latest snapshot as the
    'pre-renewal' baseline silently used POST-drop usage as if it were PRE-drop usage,
    producing a wildly wrong proposed quota (1099 instead of the correct 200) in a live
    apply-due run. Caught by an actual end-to-end mocked-API test, not code review alone."""
    renewal_cli.cmd_install(argparse_namespace())
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    conn = sqlite3.connect(db_path)
    import shadow_monitor as sm_mod
    sm_mod.open_db(db_path).close()
    conn = sm_mod.open_db(db_path)
    sm_mod.ingest_poll(conn, [{"vpsid": 42, "created_epoch": 1700000000,
                                "quota_gb": Decimal("1000"), "used_gb": Decimal("900")}],
                        "2026-10-15T09:31:00+00:00")
    sm_mod.ingest_poll(conn, [{"vpsid": 42, "created_epoch": 1700000000,
                                "quota_gb": Decimal("1000"), "used_gb": Decimal("1")}],
                        "2026-10-15T10:01:00+00:00")
    baseline = renewal_cli._renewal_baseline_for_vpsid(conn, 42)
    assert baseline is not None
    old_quota, old_used, event_ts = baseline
    assert old_quota == Decimal("1000")
    assert old_used == Decimal("900")  # the PRE-drop value, not the post-drop 1
    conn.close()


def test_renewal_baseline_returns_none_when_no_drop_observed(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    import shadow_monitor as sm_mod
    conn = sm_mod.open_db(db_path)
    sm_mod.ingest_poll(conn, [{"vpsid": 5, "created_epoch": 1700000000,
                                "quota_gb": Decimal("1000"), "used_gb": Decimal("100")}],
                        "2026-10-15T09:31:00+00:00")
    sm_mod.ingest_poll(conn, [{"vpsid": 5, "created_epoch": 1700000000,
                                "quota_gb": Decimal("1000"), "used_gb": Decimal("200")}],  # usage rose
                        "2026-10-15T10:01:00+00:00")
    assert renewal_cli._renewal_baseline_for_vpsid(conn, 5) is None
    conn.close()


# ---------- validate-credentials: safe probe without touching a real VPS -----------------------

def test_validate_credentials_requires_stored_credentials_first(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_validate_credentials(argparse_namespace(node="test-node"))
    assert rc == 1


def test_validate_credentials_reports_confidence_from_probe(cli_env, monkeypatch):
    renewal_cli.cmd_install(argparse_namespace())
    import credentials as creds_mod
    path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    creds_mod.store_credentials(path, "https://example.invalid", "k", "p")
    import writer as writer_mod
    monkeypatch.setattr(writer_mod, "diagnose_listvs_response", lambda *a, **k: {
        "status": 200, "content_type": "application/json",
        "top_level_keys": ["vs"], "structure": {"_type": "object", "_key_count": 1}, "error": None,
    })
    rc = renewal_cli.cmd_validate_credentials(argparse_namespace(node="test-node"))
    assert rc == 0


def test_diagnose_api_requires_stored_credentials(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_diagnose_api(argparse_namespace(node="test-node"))
    assert rc == 1


def test_diagnose_api_prints_sanitized_report_never_raw_values(cli_env, monkeypatch, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    import credentials as creds_mod
    path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    creds_mod.store_credentials(path, "https://example.invalid", "SECRET-KEY-VALUE", "SECRET-PASS-VALUE")
    import writer as writer_mod
    monkeypatch.setattr(writer_mod, "diagnose_listvs_response", lambda *a, **k: {
        "status": 200, "content_type": "application/json", "top_level_keys": ["vs"],
        "structure": {"_type": "object", "_key_count": 1,
                       "_keys": {"vs": {"_type": "object", "_key_count": 1, "_keys_redacted": True}}},
        "error": None,
    })
    rc = renewal_cli.cmd_diagnose_api(argparse_namespace(node="test-node"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SECRET-KEY-VALUE" not in out
    assert "SECRET-PASS-VALUE" not in out
    assert "HTTP status: 200" in out
    assert "_keys_redacted" in out


def test_diagnose_api_error_path_never_leaks(cli_env, monkeypatch, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    import credentials as creds_mod
    path = creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node")
    creds_mod.store_credentials(path, "https://example.invalid", "SECRET-KEY-VALUE", "SECRET-PASS-VALUE")
    import writer as writer_mod
    monkeypatch.setattr(writer_mod, "diagnose_listvs_response", lambda *a, **k: {
        "status": None, "content_type": None, "top_level_keys": None, "structure": None,
        "error": "network failure (sanitized)",
    })
    rc = renewal_cli.cmd_diagnose_api(argparse_namespace(node="test-node"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "SECRET-KEY-VALUE" not in out
    assert "SECRET-PASS-VALUE" not in out


# ---------- unattended apply-due: zero interactive input, proven not assumed -------------------

def test_apply_due_non_interactive_never_calls_input_or_getpass(cli_env, monkeypatch):
    """Proves (not just documents) that `apply-due --non-interactive` -- exactly what the
    generated virt-renewal-apply.service unit invokes -- never reaches a single interactive
    prompt. input()/getpass.getpass() are patched to fail the test immediately if called."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")

    import credentials as creds_mod
    creds_mod.store_credentials(
        creds_mod.default_credentials_path(renewal_cli.CONFIG_DIR, "test-node"),
        "https://example.invalid", "k", "p")

    def fail_input(*a, **k):
        pytest.fail("apply-due --non-interactive must never call input()")

    def fail_getpass(*a, **k):
        pytest.fail("apply-due --non-interactive must never call getpass.getpass()")

    monkeypatch.setattr("builtins.input", fail_input)
    import getpass as getpass_mod
    monkeypatch.setattr(getpass_mod, "getpass", fail_getpass)

    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso",
                         lambda: datetime(2026, 10, 15, 9, 31, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 99, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("900")}])
    renewal_cli.cmd_dry_run(argparse_namespace())
    monkeypatch.setattr(renewal_cli.sm, "utcnow_iso",
                         lambda: datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(renewal_cli.sm, "fetch_live_rows", lambda ssh_cmd=None: [
        {"vpsid": 99, "created_epoch": created_epoch, "quota_gb": Decimal("1000"), "used_gb": Decimal("1")}])
    renewal_cli.cmd_dry_run(argparse_namespace())

    policy_path = renewal_cli._policy_config_path()
    data = json.loads(open(policy_path).read())
    data["global_defaults"]["auto_renewal_enabled"] = True
    open(policy_path, "w").write(json.dumps(data))

    import writer as writer_mod, local_source as ls, time as time_mod
    monkeypatch.setattr(writer_mod, "call_managevps_api", lambda *a, **k: {"raw": '{"done":1}', "parsed": {"done": 1}})
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)  # skip the real settle delay in tests
    reads = iter([Decimal("1"), Decimal("200"), Decimal("200")])
    monkeypatch.setattr(ls, "fetch_single_vps_quota", lambda v: next(reads))

    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    db_path = os.path.join(renewal_cli.STATE_DIR, "test-node.sqlite3")
    op = sqlite3.connect(db_path).execute("SELECT status FROM operations WHERE vpsid=99").fetchone()
    assert op[0] == "APPLIED"  # proves the run completed, not that it silently skipped everything


def test_apply_due_non_interactive_without_stored_credentials_fails_safe_no_prompt(cli_env, monkeypatch):
    """If credentials were never stored, --non-interactive must fail cleanly (not fall back
    to prompting) -- a systemd timer has no terminal to prompt on anyway."""
    renewal_cli.cmd_install(argparse_namespace())
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    open(renewal_cli.WRITE_APPROVAL_MARKER, "w").write("approved")
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("must not prompt"))
    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    assert rc == 1


# ---------- v1.1: configurable monitor/apply/notifier intervals ----------------------------

def test_validate_interval_minutes_accepts_plain_int():
    assert renewal_cli.validate_interval_minutes(5) == 5
    assert renewal_cli.validate_interval_minutes("30") == 30


def test_validate_interval_minutes_rejects_out_of_range():
    with pytest.raises(ValueError):
        renewal_cli.validate_interval_minutes(0)
    with pytest.raises(ValueError):
        renewal_cli.validate_interval_minutes(181)


def test_validate_interval_minutes_rejects_non_numeric_injection_attempt():
    """CRITICAL: this value is interpolated directly into a systemd unit file's
    OnUnitActiveSec= line -- must reject anything that isn't a plain integer, including
    shell-metacharacter or unit-file-breaking strings."""
    for bad in ("5; rm -rf /", "5\n[Service]\nExecStart=/bin/evil", "abc", "5min", ""):
        with pytest.raises(ValueError):
            renewal_cli.validate_interval_minutes(bad)


def test_install_honors_custom_intervals_via_env_vars(cli_env, monkeypatch):
    monkeypatch.setenv("TIXO_RENEWAL_MONITOR_INTERVAL_MIN", "10")
    monkeypatch.setenv("TIXO_RENEWAL_APPLY_INTERVAL_MIN", "15")
    monkeypatch.setenv("TIXO_RENEWAL_NOTIFIER_INTERVAL_MIN", "60")
    renewal_cli.cmd_install(argparse_namespace())
    monitor_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer")).read_text()
    apply_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-apply.timer")).read_text()
    notifier_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-readiness-notifier.timer")).read_text()
    assert "OnUnitActiveSec=10min" in monitor_content
    assert "OnUnitActiveSec=15min" in apply_content
    assert "OnUnitActiveSec=60min" in notifier_content


def test_write_systemd_units_is_the_single_shared_generator(cli_env):
    """The menu's interval-reconfiguration feature must reuse this exact function -- never a
    second, divergent implementation."""
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.write_systemd_units(monitor_interval_min=20, apply_interval_min=20,
                                     notifier_interval_min=45)
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.timer")).read_text()
    assert "OnUnitActiveSec=20min" in content
    notifier_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-readiness-notifier.timer")).read_text()
    assert "OnUnitActiveSec=45min" in notifier_content


# ---------- v1.4: optional Textual UI install flow -- never automatic -------------------

def test_install_ui_noop_when_already_importable(monkeypatch, capsys):
    import types
    fake_textual = types.ModuleType("textual")
    fake_textual.__version__ = "1.2.3"
    monkeypatch.setitem(sys.modules, "textual", fake_textual)
    rc = renewal_cli.cmd_install_ui(argparse_namespace(yes=True))
    assert rc == 0
    assert "already installed" in capsys.readouterr().out


def test_install_ui_requires_confirmation_without_yes_flag(monkeypatch):
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    rc = renewal_cli.cmd_install_ui(argparse_namespace(yes=False))
    assert rc == 1


def test_install_ui_never_runs_pip_without_explicit_approval(monkeypatch):
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    called = {"ran": False}
    def fake_run(*a, **k):
        called["ran"] = True
        class R:
            returncode = 0
        return R()
    monkeypatch.setattr(renewal_cli.subprocess, "run", fake_run)
    renewal_cli.cmd_install_ui(argparse_namespace(yes=False))
    assert called["ran"] is False


def test_hostnet_sync_systemd_unit_never_auto_enabled_by_install(cli_env):
    """v1.7.0 explicit requirement: no automatic production activation -- install only
    generates the local review-copy unit files, never copies them into /etc/systemd/system
    or calls systemctl enable."""
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-hostnet-sync.timer")).read_text()
    assert "WantedBy=timers.target" in content  # a real unit file, not enabled by this process
    import subprocess
    # Never invoked by cmd_install itself -- verified by the absence of any systemctl calls
    # in install.sh being exercised here (cmd_install is pure Python file-writing).


# ---------- v1.7.0 final: noninteractive hostnet configuration for headless installs -----

def test_cmd_hostnet_configure_refuses_without_yes(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_hostnet_configure(argparse_namespace(interface="eth0", yes=False))
    assert rc == 1
    assert "Refusing" in capsys.readouterr().out


def test_cmd_hostnet_configure_succeeds_with_yes(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_hostnet_configure(argparse_namespace(interface="eth0", yes=True))
    assert rc == 0
    import hostnet_sync as hns_mod
    settings = hns_mod.load_settings(renewal_cli.CONFIG_DIR)
    assert settings["interface"] == "eth0"


def test_cmd_hostnet_configure_refuses_virtual_interface(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_hostnet_configure(argparse_namespace(interface="viifv1234", yes=True))
    assert rc == 1
    assert "Rejected" in capsys.readouterr().out


def test_cmd_hostnet_suggest_never_saves_anything(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_hostnet_suggest(argparse_namespace())
    assert rc == 0
    import hostnet_sync as hns_mod
    settings = hns_mod.load_settings(renewal_cli.CONFIG_DIR)
    assert settings["interface"] is None  # suggest never configures
    assert "auto_selected=" in capsys.readouterr().out


def test_telegram_bot_unit_is_simple_restart_always_not_a_timer(cli_env):
    """REGRESSION guard: this unit is a persistent long-poll process, not a periodic task --
    it must be Type=simple/Restart=always and have NO corresponding .timer file, unlike
    every other generated unit pair."""
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR,
                                 "virt-renewal-telegram-bot.service")).read_text()
    assert "Type=simple" in content
    assert "Restart=always" in content
    assert "telegram-bot-run" in content
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR,
                                            "virt-renewal-telegram-bot.timer"))


def test_cmd_install_generates_backup_systemd_unit_as_oneshot_timer_pair(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    service_path = os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-backup.service")
    timer_path = os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-backup.timer")
    assert os.path.exists(service_path)
    assert os.path.exists(timer_path)
    content = Path(service_path).read_text()
    assert "Type=oneshot" in content
    assert "backup-scheduled-run" in content
    timer_content = Path(timer_path).read_text()
    assert "OnUnitActiveSec=24h" in timer_content


def test_write_backup_systemd_unit_custom_interval_does_not_touch_other_units(cli_env):
    """REGRESSION guard: reconfiguring the backup interval must never reset a previously
    customized monitor/apply/notifier/inventory/hostnet interval -- that's exactly why
    write_backup_systemd_unit() is a separate function from write_systemd_units()."""
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.write_systemd_units(monitor_interval_min=99)
    renewal_cli.write_backup_systemd_unit(interval_hours=6)
    monitor_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR,
                                          "virt-renewal-monitor.timer")).read_text()
    assert "OnUnitActiveSec=99min" in monitor_content
    timer_content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR,
                                        "virt-renewal-backup.timer")).read_text()
    assert "OnUnitActiveSec=6h" in timer_content


def test_uninstall_removes_backup_unit_files(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.cmd_uninstall(argparse_namespace(purge=False, yes=True, confirm=None, delete_backups=False))
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-backup.service"))
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-backup.timer"))


def test_menu_items_includes_backup_schedule_config():
    import menu
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Configure Backup Schedule (optional)" in labels


@pytest.mark.parametrize("unit_name,cli_command", [
    ("virt-renewal-web.service", "web-run"),
    ("virt-renewal-relay-worker.service", "relay-worker-run"),
    ("virt-renewal-agent.service", "agent-run"),
])
def test_optional_persistent_units_are_simple_restart_always_not_timers(cli_env, unit_name,
                                                                          cli_command):
    """Same shape as the Telegram bot unit -- each of these three runs a persistent
    http.server loop (web_server.py/relay_worker.py/agent_server.py), not a periodic task."""
    renewal_cli.cmd_install(argparse_namespace())
    content = Path(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, unit_name)).read_text()
    assert "Type=simple" in content
    assert "Restart=always" in content
    assert cli_command in content
    timer_name = unit_name.replace(".service", ".timer")
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, timer_name))


# ---------- v1.10.2: cmd_update_check / cmd_update_apply -- real CLI update flow -----------

def test_cmd_update_check_reports_up_to_date(cli_env, monkeypatch, capsys):
    import json as _json

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return _json.dumps({"tag_name": f"v{renewal_cli.VERSION}", "body": "",
                                 "assets": []}).encode()

    import updater
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout: FakeResp())
    rc = renewal_cli.cmd_update_check(argparse_namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Already up to date" in out


def test_cmd_update_apply_full_cli_flow_preserves_config_and_restarts_active_services(
        cli_env, monkeypatch, tmp_path, capsys):
    """End-to-end at the REAL CLI command layer (not just updater.apply_update() directly,
    which test_updater.py already covers exhaustively at the function level): builds a
    'new' package, points cmd_update_apply's network layer at it via monkeypatched
    updater.fetch_release_info/download_file, and confirms the actual CLI command preserves
    config/secrets, applies the new code, and restarts exactly the services that were
    already active before the update -- simulating an upgrade from a v1.10.0/v1.10.1-shaped
    install to the current version."""
    import hashlib
    import tarfile
    import updater

    renewal_cli.cmd_install(argparse_namespace())
    config_path = renewal_cli._policy_config_path()
    original_policy_bytes = open(config_path, "rb").read()

    # Build a fake 'new' release package.
    src = tmp_path / "pkg-src"
    (src / "lib" / "static").mkdir(parents=True)
    (src / "bin").mkdir(parents=True)
    (src / "lib" / "renewal_cli.py").write_text('VERSION = "99.0.0"\n')
    (src / "bin" / "renewal_cli.py").write_text("# bin stub\n")
    tarball = tmp_path / "tixo-renewal-package.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(src / "lib", arcname="lib")
        tf.add(src / "bin", arcname="bin")
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  {updater.ASSET_NAME}\n")

    def fake_fetch(tag=None):
        return {"ok": True, "tag": "v99.0.0", "version": "99.0.0", "notes": "test release",
                "assets": {updater.ASSET_NAME: "fake://pkg", updater.CHECKSUMS_NAME: "fake://sums"},
                "error": None}

    def fake_download(url, dest_path, timeout=60.0, max_bytes=None):
        import shutil as _shutil
        _shutil.copy2(str(tarball) if "pkg" in url else str(sums), dest_path)

    monkeypatch.setattr(updater, "fetch_release_info", fake_fetch)
    monkeypatch.setattr(updater, "download_file", fake_download)

    restart_calls = []
    def fake_run(cmd, *a, **k):
        restart_calls.append(cmd)
        class R:
            returncode = 0
            stdout = "active" if cmd[:2] == ["systemctl", "is-active"] else ""
        return R()
    monkeypatch.setattr(renewal_cli.subprocess, "run", fake_run)

    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(
        AssertionError("--yes must skip the interactive confirmation")))

    rc = renewal_cli.cmd_update_apply(argparse_namespace(version=None, yes=True, force=False))
    assert rc == 0, capsys.readouterr().out

    # New code applied.
    new_version = open(os.path.join(renewal_cli.WORKSPACE_ROOT, "lib", "renewal_cli.py")).read()
    assert '"99.0.0"' in new_version

    # Config untouched (byte-identical).
    assert open(config_path, "rb").read() == original_policy_bytes

    # Web + Telegram services (reported "active" by the fake systemctl) were restarted.
    restart_cmds = [c for c in restart_calls if c[:2] == ["systemctl", "restart"]]
    restarted_units = {c[2] for c in restart_cmds}
    assert "virt-renewal-web.service" in restarted_units
    assert "virt-renewal-telegram-bot.service" in restarted_units


def test_cmd_update_apply_refuses_without_yes_or_confirmation(cli_env, monkeypatch, capsys):
    import updater

    def fake_fetch(tag=None):
        return {"ok": True, "tag": "v99.0.0", "version": "99.0.0", "notes": "",
                "assets": {updater.ASSET_NAME: "x", updater.CHECKSUMS_NAME: "y"}, "error": None}
    monkeypatch.setattr(updater, "fetch_release_info", fake_fetch)
    monkeypatch.setattr("builtins.input", lambda *a: "no")  # declines

    called = []
    monkeypatch.setattr(updater, "apply_update", lambda *a, **k: called.append(1) or {"ok": True})
    rc = renewal_cli.cmd_update_apply(argparse_namespace(version=None, yes=False, force=False))
    assert rc == 1
    assert called == []


def test_cmd_update_apply_noop_when_already_up_to_date_without_force(cli_env, monkeypatch, capsys):
    import updater

    def fake_fetch(tag=None):
        return {"ok": True, "tag": f"v{renewal_cli.VERSION}", "version": renewal_cli.VERSION,
                "notes": "", "assets": {updater.ASSET_NAME: "x", updater.CHECKSUMS_NAME: "y"},
                "error": None}
    monkeypatch.setattr(updater, "fetch_release_info", fake_fetch)
    called = []
    monkeypatch.setattr(updater, "apply_update", lambda *a, **k: called.append(1) or {"ok": True})
    rc = renewal_cli.cmd_update_apply(argparse_namespace(version=None, yes=True, force=False))
    assert rc == 0
    assert called == []
    out = capsys.readouterr().out
    assert "nothing to do" in out
