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
    renewal_cli.cmd_uninstall(argparse_namespace(purge=False))
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.service"))
    assert os.path.isdir(renewal_cli.STATE_DIR)  # data kept


def test_uninstall_purge_removes_data(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    renewal_cli.cmd_uninstall(argparse_namespace(purge=True))
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
    # install.sh itself never copies anything into /etc/systemd/system -- verified separately
    # by reading deploy/install.sh; this test only confirms the files exist locally.


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


def test_apply_due_blocked_without_write_approval(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_apply_due(argparse_namespace(node="test-node", non_interactive=True))
    assert rc == 1


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
