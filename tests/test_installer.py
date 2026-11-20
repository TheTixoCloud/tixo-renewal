"""Tests for installer.py (v1.8.0 Part 17): detect/upgrade/repair/rollback/health-check,
port/HTTPS-prerequisite checks, and the explicit-trigger-only automatic certificate helper.
"""
import os
import socket
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import installer
import nodes as nodes_mod
import renewal_cli


def argparse_namespace(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


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


# ---------- detect_installation_state ------------------------------------------------------------

def test_detect_fresh_install(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    os.makedirs(config_dir)
    os.makedirs(state_dir)
    result = installer.detect_installation_state(config_dir, state_dir)
    assert result["existing"] is False


def test_detect_existing_install_via_nodes_json(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    os.makedirs(config_dir)
    os.makedirs(state_dir)
    nodes_mod.write_default_config(os.path.join(config_dir, "nodes.json"))
    result = installer.detect_installation_state(config_dir, state_dir)
    assert result["existing"] is True
    assert result["has_nodes"] is True


def test_detect_existing_install_via_db_files(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    os.makedirs(config_dir)
    os.makedirs(state_dir)
    open(os.path.join(state_dir, "self.sqlite3"), "w").close()
    result = installer.detect_installation_state(config_dir, state_dir)
    assert result["existing"] is True
    assert result["db_files"] == ["self.sqlite3"]


# ---------- check_port_available ------------------------------------------------------------------

def test_check_port_available_true_for_unused_port():
    assert installer.check_port_available("127.0.0.1", 0) is True  # port 0 always "available" (OS-assigned)


def test_check_port_available_false_for_bound_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert installer.check_port_available("127.0.0.1", port) is False
    finally:
        s.close()


# ---------- validate_https_prerequisites ----------------------------------------------------------

def _generate_self_signed_cert(tmp_path):
    cert_path = str(tmp_path / "cert.pem")
    key_path = str(tmp_path / "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key_path,
         "-out", cert_path, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
        check=True, capture_output=True)
    return cert_path, key_path


def test_validate_https_prerequisites_ok_for_valid_cert(tmp_path):
    cert_path, key_path = _generate_self_signed_cert(tmp_path)
    result = installer.validate_https_prerequisites(cert_path, key_path, "127.0.0.1", 0)
    assert result["ok"] is True
    assert result["problems"] == []


def test_validate_https_prerequisites_missing_files(tmp_path):
    result = installer.validate_https_prerequisites(
        str(tmp_path / "nope-cert.pem"), str(tmp_path / "nope-key.pem"), "127.0.0.1", 0)
    assert result["ok"] is False
    assert any("not found" in p for p in result["problems"])


def test_validate_https_prerequisites_rejects_missing_pair(tmp_path):
    cert_path, _ = _generate_self_signed_cert(tmp_path)
    result = installer.validate_https_prerequisites(cert_path, None, "127.0.0.1", 0)
    assert result["ok"] is False


def test_validate_https_prerequisites_detects_port_conflict(tmp_path):
    cert_path, key_path = _generate_self_signed_cert(tmp_path)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        result = installer.validate_https_prerequisites(cert_path, key_path, "127.0.0.1", port)
        assert result["ok"] is False
        assert any("already in use" in p for p in result["problems"])
    finally:
        s.close()


def test_validate_https_prerequisites_rejects_mismatched_pair(tmp_path):
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    cert1, key1 = _generate_self_signed_cert(dir_a)
    cert2, key2 = _generate_self_signed_cert(dir_b)
    result = installer.validate_https_prerequisites(cert1, key2, "127.0.0.1", 0)
    assert result["ok"] is False


# ---------- request_automatic_certificate (explicit-trigger only) --------------------------------

def test_request_automatic_certificate_blocked_without_certbot(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    result = installer.request_automatic_certificate("example.com", "ops@example.com",
                                                        str(tmp_path))
    assert result["ok"] is False
    assert result["attempted"] is False
    assert "certbot is not installed" in result["error"]


def test_request_automatic_certificate_blocked_on_port_80_conflict(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/certbot")
    monkeypatch.setattr(installer, "check_port_available", lambda host, port: False)
    result = installer.request_automatic_certificate("example.com", "ops@example.com",
                                                        str(tmp_path))
    assert result["ok"] is False
    assert result["attempted"] is False
    assert "port 80" in result["error"]


def test_request_automatic_certificate_blocked_on_missing_webroot(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/certbot")
    monkeypatch.setattr(installer, "check_port_available", lambda host, port: True)
    result = installer.request_automatic_certificate("example.com", "ops@example.com",
                                                        str(tmp_path / "nonexistent"))
    assert result["ok"] is False
    assert result["attempted"] is False


def test_request_automatic_certificate_attempts_via_injected_run_fn(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/certbot")
    monkeypatch.setattr(installer, "check_port_available", lambda host, port: True)
    calls = []
    class _Result:
        returncode = 0
        stderr = ""
    def fake_run(cmd):
        calls.append(cmd)
        return _Result()
    result = installer.request_automatic_certificate("example.com", "ops@example.com",
                                                        str(tmp_path), run_fn=fake_run)
    assert result["ok"] is True
    assert result["attempted"] is True
    assert calls[0][0] == "/usr/bin/certbot"
    assert "example.com" in calls[0]


def test_request_automatic_certificate_reports_failure_from_run_fn(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/certbot")
    monkeypatch.setattr(installer, "check_port_available", lambda host, port: True)
    class _Result:
        returncode = 1
        stderr = "challenge failed"
    result = installer.request_automatic_certificate("example.com", "ops@example.com",
                                                        str(tmp_path), run_fn=lambda cmd: _Result())
    assert result["ok"] is False
    assert result["attempted"] is True
    assert "challenge failed" in result["error"]


# ---------- upgrade / repair / rollback (requires a real install) --------------------------------

def test_upgrade_preserves_existing_nodes_json_edits(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    nodes_path = os.path.join(cli_env["config_dir"], "nodes.json")
    import json
    data = json.loads(open(nodes_path).read())
    data["nodes"][0]["node_id"] = "operator-renamed"
    open(nodes_path, "w").write(json.dumps(data))

    result = installer.upgrade(cli_env["config_dir"], cli_env["state_dir"], cli_env["backup_dir"])
    assert result["ok"] is True
    assert os.path.exists(result["backup"])

    reloaded = json.loads(open(nodes_path).read())
    assert reloaded["nodes"][0]["node_id"] == "operator-renamed"


def test_upgrade_creates_a_backup_archive_first(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    import backup as backup_mod
    assert backup_mod.list_backup_archives(cli_env["backup_dir"]) == []
    installer.upgrade(cli_env["config_dir"], cli_env["state_dir"], cli_env["backup_dir"])
    assert len(backup_mod.list_backup_archives(cli_env["backup_dir"])) == 1


def test_repair_recreates_missing_systemd_units(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    import shutil as _shutil
    _shutil.rmtree(renewal_cli.SYSTEMD_OUT_DIR)
    assert not os.path.exists(renewal_cli.SYSTEMD_OUT_DIR)

    result = installer.repair(cli_env["config_dir"], cli_env["state_dir"], cli_env["backup_dir"])
    assert result["ok"] is True
    assert os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, "virt-renewal-monitor.service"))


def test_repair_fixes_loosened_secret_file_permissions(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    secrets_dir = os.path.join(cli_env["config_dir"], "telegram")
    os.makedirs(secrets_dir, exist_ok=True)
    secrets_path = os.path.join(secrets_dir, "secrets.json")
    with open(secrets_path, "w") as f:
        f.write("{}")
    os.chmod(secrets_path, 0o644)

    result = installer.repair(cli_env["config_dir"], cli_env["state_dir"], cli_env["backup_dir"])
    assert secrets_path in result["fixed_permissions"]
    assert stat.S_IMODE(os.stat(secrets_path).st_mode) == 0o600


def test_rollback_restores_from_backup_and_safety_copies_current_state(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    nodes_path = os.path.join(cli_env["config_dir"], "nodes.json")
    import json

    # Snapshot the "good" state.
    good_backup = installer.backup_before_change(cli_env["config_dir"], cli_env["state_dir"],
                                                    cli_env["backup_dir"], label="good")

    # Simulate a bad change.
    data = json.loads(open(nodes_path).read())
    data["nodes"][0]["node_id"] = "BROKEN"
    open(nodes_path, "w").write(json.dumps(data))

    result = installer.rollback(good_backup, cli_env["config_dir"], cli_env["state_dir"])
    assert "nodes.json" in result["restored_config"]
    assert result["safety_backup"] is not None

    restored = json.loads(open(nodes_path).read())
    assert restored["nodes"][0]["node_id"] != "BROKEN"


def test_rollback_refuses_corrupt_archive(cli_env, tmp_path):
    bad_archive = tmp_path / "bad.tar.gz"
    bad_archive.write_bytes(b"not a real archive")
    with pytest.raises(ValueError):
        installer.rollback(str(bad_archive), cli_env["config_dir"], cli_env["state_dir"])


# ---------- health_check --------------------------------------------------------------------------

def test_health_check_ok_after_fresh_install(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    result = installer.health_check(cli_env["config_dir"], cli_env["state_dir"])
    assert result["ok"] is True
    assert result["problems"] == []


def test_health_check_detects_corrupt_policy_json(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    policy_path = os.path.join(cli_env["config_dir"], "policy.json")
    with open(policy_path, "w") as f:
        f.write("{not valid json")
    result = installer.health_check(cli_env["config_dir"], cli_env["state_dir"])
    assert result["ok"] is False
    assert any("policy.json" in p for p in result["problems"])


# ---------- renewal_cli.py install-* CLI commands -------------------------------------------------

def test_cmd_install_detect_reports_fresh(cli_env, capsys):
    rc = renewal_cli.cmd_install_detect(argparse_namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert '"existing": false' in out


def test_cmd_install_upgrade_succeeds_after_install(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_install_upgrade(argparse_namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Upgrade succeeded" in out


def test_cmd_install_repair_succeeds_after_install(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_install_repair(argparse_namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Repair succeeded" in out


def test_cmd_install_rollback_requires_yes_flag(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_install_rollback(argparse_namespace(backup_file="x.tar.gz", yes=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "OVERWRITE" in out


def test_cmd_install_rollback_restores_with_yes_flag(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    archive = installer.backup_before_change(cli_env["config_dir"], cli_env["state_dir"],
                                                cli_env["backup_dir"], label="good")
    rc = renewal_cli.cmd_install_rollback(
        argparse_namespace(backup_file=os.path.basename(archive), yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Restored config" in out


def test_cmd_install_health_check_ok_after_install_and_activation(cli_env, capsys, monkeypatch):
    """v1.10.3: cmd_install_health_check now also checks real systemd state
    (check_systemd=True). A plain cmd_install() alone does NOT activate any real systemd
    unit (that's a separate, explicit step -- see test_service_activation.py), so this test
    simulates the state AFTER that activation step succeeded, via the same
    service_activation._systemctl mock point every other systemd-aware test in this project
    uses -- never touching the real host's systemd."""
    import service_activation as sa
    monkeypatch.setattr(sa, "_systemctl",
                         lambda *a, **k: {"ok": True, "stdout": "enabled" if a and a[0] == "is-enabled"
                                          else ("active" if a and a[0] == "is-active" else ""),
                                          "stderr": ""})
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_install_health_check(argparse_namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert '"ok": true' in out


def test_cmd_install_health_check_flags_unactivated_core_timers(cli_env, capsys, monkeypatch):
    """The exact regression this was added to prevent: a fresh install whose core timers
    were never actually installed/enabled into real systemd must be reported as UNHEALTHY,
    not silently 'ok' -- this is what would have surfaced the `ir2` problem immediately
    instead of requiring manual systemctl/journal investigation."""
    import service_activation as sa
    monkeypatch.setattr(sa, "_systemctl",
                         lambda *a, **k: {"ok": False, "stdout": "disabled" if a and a[0] == "is-enabled"
                                          else "inactive", "stderr": ""})
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_install_health_check(argparse_namespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert '"ok": false' in out
    assert "is not enabled" in out


def test_cmd_install_health_check_flags_a_failed_service(cli_env, capsys, monkeypatch):
    import service_activation as sa

    def fake_systemctl(*args, **kwargs):
        if args and args[0] == "is-enabled":
            return {"ok": True, "stdout": "enabled", "stderr": ""}
        if args and args[0] == "is-active":
            return {"ok": True, "stdout": "active", "stderr": ""}
        if args and args[0] == "is-failed":
            return {"ok": False, "stdout": "failed", "stderr": ""}
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(sa, "_systemctl", fake_systemctl)
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_install_health_check(argparse_namespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert '"ok": false' in out
    assert "FAILED state" in out


def test_health_check_direct_call_defaults_to_no_systemd_check(cli_env):
    """installer.health_check()'s own default (check_systemd=False) must stay unaffected by
    whatever the real host's systemd state is -- this is the exact function updater.py's
    apply_update() uses as its own default post-update rollback gate, which must never fail
    an update just because this particular install never had its timers activated."""
    renewal_cli.cmd_install(argparse_namespace())
    result = installer.health_check(cli_env["config_dir"], cli_env["state_dir"])
    assert result["ok"] is True
    assert result["problems"] == []
    assert "systemd" not in result


# ---------- menu.action_install wizard (fresh/upgrade/repair dispatch) ---------------------------

def test_action_install_runs_fresh_install_when_none_detected(cli_env, monkeypatch, capsys):
    import menu
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", cli_env["config_dir"])
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", cli_env["state_dir"])
    monkeypatch.setattr(menu.renewal_cli, "BACKUP_DIR", cli_env["backup_dir"])
    style = menu.Style(enabled=False)
    menu.action_install(style)
    out = capsys.readouterr().out
    assert "running a fresh install" in out
    assert os.path.exists(os.path.join(cli_env["state_dir"], "test-node.sqlite3"))


def test_action_install_offers_upgrade_for_existing_install(cli_env, monkeypatch, capsys):
    import menu
    renewal_cli.cmd_install(argparse_namespace())
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", cli_env["config_dir"])
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", cli_env["state_dir"])
    monkeypatch.setattr(menu.renewal_cli, "BACKUP_DIR", cli_env["backup_dir"])
    monkeypatch.setattr("builtins.input", lambda *a: "u")
    style = menu.Style(enabled=False)
    menu.action_install(style)
    out = capsys.readouterr().out
    assert "Existing installation detected" in out
    assert "Upgrade succeeded" in out
    assert "Post-change health check: OK" in out


def test_action_install_offers_repair_for_existing_install(cli_env, monkeypatch, capsys):
    import menu
    renewal_cli.cmd_install(argparse_namespace())
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", cli_env["config_dir"])
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", cli_env["state_dir"])
    monkeypatch.setattr(menu.renewal_cli, "BACKUP_DIR", cli_env["backup_dir"])
    monkeypatch.setattr("builtins.input", lambda *a: "r")
    style = menu.Style(enabled=False)
    menu.action_install(style)
    out = capsys.readouterr().out
    assert "Repair succeeded" in out


def test_uninstall_optional_module_rejects_unknown_module(cli_env):
    with pytest.raises(ValueError):
        installer.uninstall_optional_module("nope", cli_env["config_dir"],
                                              renewal_cli.SYSTEMD_OUT_DIR)


@pytest.mark.parametrize("module_name,settings_module,settings_loader,unit_name", [
    ("telegram", "telegram_config", "load_telegram_settings", "virt-renewal-telegram-bot.service"),
    ("web", "web_config", "load_web_settings", "virt-renewal-web.service"),
    ("agent", "agent_config", "load_agent_settings", "virt-renewal-agent.service"),
])
def test_uninstall_optional_module_disables_and_removes_unit(cli_env, module_name,
                                                                settings_module, settings_loader,
                                                                unit_name):
    renewal_cli.cmd_install(argparse_namespace())  # generates the systemd units
    assert os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, unit_name))

    import importlib
    mod = importlib.import_module(settings_module)
    # Enable it first so disabling is a real, observable state change.
    settings = getattr(mod, settings_loader)(cli_env["config_dir"])
    settings.enabled = True
    save_fn_name = settings_loader.replace("load_", "save_")
    getattr(mod, save_fn_name)(cli_env["config_dir"], settings)

    result = installer.uninstall_optional_module(module_name, cli_env["config_dir"],
                                                    renewal_cli.SYSTEMD_OUT_DIR)
    assert result["disabled"] is True
    assert result["secrets_purged"] is False
    assert unit_name in result["units_removed"]
    assert not os.path.exists(os.path.join(renewal_cli.SYSTEMD_OUT_DIR, unit_name))

    reloaded = getattr(mod, settings_loader)(cli_env["config_dir"])
    assert reloaded.enabled is False


def test_uninstall_optional_module_keeps_secrets_by_default(cli_env):
    import telegram_config as tc
    secrets_path = tc.default_secrets_path(cli_env["config_dir"])
    tc.store_telegram_secrets(secrets_path, bot_token="123:ABC")
    installer.uninstall_optional_module("telegram", cli_env["config_dir"],
                                          renewal_cli.SYSTEMD_OUT_DIR)
    assert os.path.exists(secrets_path)


def test_uninstall_optional_module_purges_secrets_when_requested(cli_env):
    import telegram_config as tc
    secrets_path = tc.default_secrets_path(cli_env["config_dir"])
    tc.store_telegram_secrets(secrets_path, bot_token="123:ABC")
    result = installer.uninstall_optional_module("telegram", cli_env["config_dir"],
                                                    renewal_cli.SYSTEMD_OUT_DIR,
                                                    purge_secrets=True)
    assert result["secrets_purged"] is True
    assert not os.path.exists(secrets_path)


def test_uninstall_optional_module_never_touches_core_renewal_config(cli_env):
    renewal_cli.cmd_install(argparse_namespace())
    nodes_path = os.path.join(cli_env["config_dir"], "nodes.json")
    before = open(nodes_path).read()
    installer.uninstall_optional_module("telegram", cli_env["config_dir"],
                                          renewal_cli.SYSTEMD_OUT_DIR)
    after = open(nodes_path).read()
    assert before == after
    assert os.path.exists(os.path.join(cli_env["state_dir"], "test-node.sqlite3"))


def test_cmd_uninstall_module_cli(cli_env, capsys):
    renewal_cli.cmd_install(argparse_namespace())
    rc = renewal_cli.cmd_uninstall_module(
        argparse_namespace(module="telegram", purge_secrets=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "disabled" in out


def test_cmd_uninstall_module_cli_rejects_unknown_module(cli_env, capsys):
    rc = renewal_cli.cmd_uninstall_module(argparse_namespace(module="nope", purge_secrets=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unknown module" in out


def test_action_install_cancel_does_nothing(cli_env, monkeypatch, capsys):
    import menu
    renewal_cli.cmd_install(argparse_namespace())
    monkeypatch.setattr(menu.renewal_cli, "CONFIG_DIR", cli_env["config_dir"])
    monkeypatch.setattr(menu.renewal_cli, "STATE_DIR", cli_env["state_dir"])
    monkeypatch.setattr(menu.renewal_cli, "BACKUP_DIR", cli_env["backup_dir"])
    monkeypatch.setattr("builtins.input", lambda *a: "b")
    style = menu.Style(enabled=False)
    menu.action_install(style)
    out = capsys.readouterr().out
    assert "Cancelled" in out
