"""Tests for uninstaller.py (v1.10.2): the real two-mode uninstall mechanism. All systemd
interaction is through an injected fake (`_systemctl` is monkeypatched) -- this suite never
calls the real `systemctl` binary or touches a real /etc/systemd/system, consistent with
"never test destructive operations on real production hosts" and "use disposable
directories" for everything filesystem-related too.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import uninstaller as un


# ---------- fixtures -----------------------------------------------------------------------

def _make_install_dir(tmp_path, with_unknown_entry=False):
    install_dir = tmp_path / "opt-tixo-renewal"
    (install_dir / "lib").mkdir(parents=True)
    (install_dir / "bin").mkdir(parents=True)
    (install_dir / "systemd").mkdir(parents=True)
    (install_dir / "renewal-config").mkdir(parents=True)
    (install_dir / "shadow-monitor-state").mkdir(parents=True)
    (install_dir / "backups").mkdir(parents=True)

    (install_dir / "lib" / "renewal_cli.py").write_text('VERSION = "1.10.2"\n')
    (install_dir / "lib" / "menu.py").write_text("# menu\n")
    (install_dir / "bin" / "renewal_cli.py").write_text("# bin stub\n")
    (install_dir / "systemd" / "virt-renewal-web.service").write_text("[Unit]\n")
    (install_dir / "renewal-config" / "policy.json").write_text('{"monthly_grant_gb": "100"}')
    (install_dir / "renewal-config" / "secrets.json").write_text('{"fake": "secret"}')
    (install_dir / "shadow-monitor-state" / "self.sqlite3").write_bytes(b"fake-db")
    (install_dir / "backups" / "2026-01-01-backup.tar.gz").write_bytes(b"fake-backup")

    if with_unknown_entry:
        (install_dir / "operator-custom-notes.txt").write_text("my own file, not yours")

    return str(install_dir)


def _patch_systemctl_noop(monkeypatch):
    """No real systemd interaction at all -- every call reports 'not installed'/success."""
    monkeypatch.setattr(un, "_systemctl",
                         lambda *a, **k: {"ok": True, "stdout": "inactive", "stderr": ""})
    monkeypatch.setattr(un, "SYSTEMD_UNIT_DIR", "/nonexistent-systemd-dir-for-tests")
    monkeypatch.setattr(un, "GLOBAL_WRAPPER_PATH", "/nonexistent-wrapper-for-tests")


# ---------- inspect_install_dir / preview (read-only) ---------------------------------------

def test_inspect_install_dir_classifies_known_entries(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    result = un.inspect_install_dir(install_dir)
    assert result["exists"] is True
    assert result["entries"]["lib"] == "code"
    assert result["entries"]["bin"] == "code"
    assert result["entries"]["systemd"] == "code"
    assert result["entries"]["renewal-config"] == "data"
    assert result["entries"]["shadow-monitor-state"] == "data"
    assert result["entries"]["backups"] == "backup"


def test_inspect_install_dir_flags_unknown_entries(tmp_path):
    install_dir = _make_install_dir(tmp_path, with_unknown_entry=True)
    result = un.inspect_install_dir(install_dir)
    assert result["entries"]["operator-custom-notes.txt"] == "UNKNOWN"


def test_inspect_install_dir_missing_directory(tmp_path):
    result = un.inspect_install_dir(str(tmp_path / "does-not-exist"))
    assert result["exists"] is False
    assert result["entries"] == {}


def test_preview_standard_never_touches_filesystem(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    before = sorted(os.listdir(install_dir))
    preview = un.preview_uninstall(install_dir, purge=False)
    after = sorted(os.listdir(install_dir))
    assert before == after  # purely read-only
    assert preview["mode"] == "standard"
    assert "lib" in preview["will_remove_entries"]
    assert "renewal-config" in preview["will_preserve_entries"]
    assert "backups" in preview["will_preserve_entries"]


def test_preview_purge_shows_data_entries_will_be_removed(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    preview = un.preview_uninstall(install_dir, purge=True)
    assert "renewal-config" in preview["will_remove_entries"]
    assert "shadow-monitor-state" in preview["will_remove_entries"]
    # Backups are STILL preserved even in a purge preview.
    assert "backups" in preview["will_preserve_entries"]
    assert "backups" not in preview["will_remove_entries"]


def test_preview_flags_unknown_entries_for_both_modes(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path, with_unknown_entry=True)
    for purge in (False, True):
        preview = un.preview_uninstall(install_dir, purge=purge)
        assert "operator-custom-notes.txt" in preview["unknown_entries_never_touched"]


# ---------- standard_uninstall ---------------------------------------------------------------

def test_standard_uninstall_removes_code_preserves_data_and_backups(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    report = un.standard_uninstall(install_dir)

    assert not os.path.exists(os.path.join(install_dir, "lib"))
    assert not os.path.exists(os.path.join(install_dir, "bin"))
    assert not os.path.exists(os.path.join(install_dir, "systemd"))
    # Data and backups survive.
    assert os.path.exists(os.path.join(install_dir, "renewal-config", "policy.json"))
    assert os.path.exists(os.path.join(install_dir, "renewal-config", "secrets.json"))
    assert os.path.exists(os.path.join(install_dir, "shadow-monitor-state", "self.sqlite3"))
    assert os.path.exists(os.path.join(install_dir, "backups", "2026-01-01-backup.tar.gz"))

    assert "lib" in report["removed"]
    assert "renewal-config" in report["preserved"]
    assert "backups" in report["preserved"]
    assert report["problems"] == []


def test_standard_uninstall_never_touches_unknown_entries(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path, with_unknown_entry=True)
    un.standard_uninstall(install_dir)
    assert os.path.exists(os.path.join(install_dir, "operator-custom-notes.txt"))


def test_standard_uninstall_on_missing_install_dir_reports_problem_not_crash(tmp_path):
    report = un.standard_uninstall(str(tmp_path / "nope"))
    assert report["problems"]
    assert "does not exist" in report["problems"][0]


def test_standard_uninstall_refuses_to_delete_lib_without_signature_file(tmp_path, monkeypatch):
    """Ownership verification: a 'lib' directory that exists but does NOT contain
    renewal_cli.py/menu.py must never be deleted just because its name matches."""
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    # Replace lib/ with something that looks nothing like this project's code.
    import shutil
    shutil.rmtree(os.path.join(install_dir, "lib"))
    os.makedirs(os.path.join(install_dir, "lib"))
    (os.path.join(install_dir, "lib"))
    with open(os.path.join(install_dir, "lib", "unrelated_file.txt"), "w") as f:
        f.write("not ours")

    report = un.standard_uninstall(install_dir)
    assert os.path.exists(os.path.join(install_dir, "lib", "unrelated_file.txt"))
    assert any("signature file" in p for p in report["problems"])


# ---------- full_purge -------------------------------------------------------------------

def test_full_purge_removes_data_but_never_backups(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    report = un.full_purge(install_dir)

    assert not os.path.exists(os.path.join(install_dir, "renewal-config"))
    assert not os.path.exists(os.path.join(install_dir, "shadow-monitor-state"))
    assert not os.path.exists(os.path.join(install_dir, "lib"))
    # Backups survive even a full purge -- requires the SEPARATE delete_backups() call.
    assert os.path.exists(os.path.join(install_dir, "backups", "2026-01-01-backup.tar.gz"))
    assert report["mode"] == "full_purge"
    assert "renewal-config" in report["removed"]
    assert "backups" in report["preserved"]


def test_full_purge_never_touches_unknown_entries(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path, with_unknown_entry=True)
    un.full_purge(install_dir)
    assert os.path.exists(os.path.join(install_dir, "operator-custom-notes.txt"))


def test_full_purge_performs_no_confirmation_itself(tmp_path, monkeypatch):
    """Confirmation (typing 'PURGE ALL TIXOCLOUD DATA') is the CALLER's responsibility --
    this function takes no confirmation argument and never prompts, matching every other
    destructive function in this codebase."""
    import inspect
    sig = inspect.signature(un.full_purge)
    assert list(sig.parameters) == ["install_dir"]


# ---------- delete_backups (separate, extra-explicit) ---------------------------------------

def test_delete_backups_is_separate_from_both_uninstall_modes(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    un.full_purge(install_dir)
    assert os.path.exists(os.path.join(install_dir, "backups"))  # still there after purge

    report = un.delete_backups(install_dir)
    assert "backups" in report["removed"]
    assert not os.path.exists(os.path.join(install_dir, "backups"))


def test_delete_backups_missing_directory_reports_problem(tmp_path):
    report = un.delete_backups(str(tmp_path / "nope"))
    assert report["problems"]


# ---------- verify_no_residue ----------------------------------------------------------------

def test_verify_no_residue_ok_after_full_purge_and_backup_deletion(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    un.full_purge(install_dir)
    un.delete_backups(install_dir)
    result = un.verify_no_residue(install_dir)
    assert result["ok"] is True
    assert result["problems"] == []


def test_verify_no_residue_detects_leftover_unit_file(tmp_path, monkeypatch):
    fake_systemd_dir = str(tmp_path / "fake-etc-systemd")
    os.makedirs(fake_systemd_dir)
    with open(os.path.join(fake_systemd_dir, "virt-renewal-web.service"), "w") as f:
        f.write("[Unit]\n")
    monkeypatch.setattr(un, "SYSTEMD_UNIT_DIR", fake_systemd_dir)
    monkeypatch.setattr(un, "_systemctl",
                         lambda *a, **k: {"ok": True, "stdout": "inactive", "stderr": ""})
    monkeypatch.setattr(un, "GLOBAL_WRAPPER_PATH", "/nonexistent-wrapper-for-tests")

    install_dir = str(tmp_path / "opt-empty")
    os.makedirs(install_dir)
    result = un.verify_no_residue(install_dir)
    assert result["ok"] is False
    assert any("virt-renewal-web.service" in p for p in result["problems"])


# ---------- stop_and_disable_owned_units / remove_owned_unit_files --------------------------

def test_stop_and_disable_skips_units_that_were_never_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(un, "SYSTEMD_UNIT_DIR", str(tmp_path / "empty-systemd-dir"))
    os.makedirs(str(tmp_path / "empty-systemd-dir"))
    calls = []
    monkeypatch.setattr(un, "_systemctl", lambda *a, **k: calls.append(a) or
                         {"ok": True, "stdout": "", "stderr": ""})
    results = un.stop_and_disable_owned_units()
    assert all(not r["acted"] for r in results.values())
    assert calls == []  # never even calls systemctl for a unit that isn't installed


def test_stop_and_disable_acts_on_installed_units_only(tmp_path, monkeypatch):
    systemd_dir = str(tmp_path / "systemd-dir")
    os.makedirs(systemd_dir)
    with open(os.path.join(systemd_dir, "virt-renewal-web.service"), "w") as f:
        f.write("[Unit]\n")
    monkeypatch.setattr(un, "SYSTEMD_UNIT_DIR", systemd_dir)
    calls = []
    monkeypatch.setattr(un, "_systemctl", lambda *a, **k: calls.append(a) or
                         {"ok": True, "stdout": "", "stderr": ""})
    results = un.stop_and_disable_owned_units()
    assert results["virt-renewal-web.service"]["acted"] is True
    assert all(not r["acted"] for name, r in results.items() if name != "virt-renewal-web.service")
    assert len(calls) == 2  # stop + disable, exactly once each, only for the installed unit


def test_remove_owned_unit_files_only_removes_known_names(tmp_path, monkeypatch):
    systemd_dir = str(tmp_path / "systemd-dir")
    os.makedirs(systemd_dir)
    with open(os.path.join(systemd_dir, "virt-renewal-web.service"), "w") as f:
        f.write("[Unit]\n")
    with open(os.path.join(systemd_dir, "totally-unrelated-other-app.service"), "w") as f:
        f.write("[Unit]\n")
    monkeypatch.setattr(un, "SYSTEMD_UNIT_DIR", systemd_dir)
    monkeypatch.setattr(un, "_systemctl", lambda *a, **k: {"ok": True, "stdout": "", "stderr": ""})

    removed = un.remove_owned_unit_files()
    assert removed == ["virt-renewal-web.service"]
    assert not os.path.exists(os.path.join(systemd_dir, "virt-renewal-web.service"))
    assert os.path.exists(os.path.join(systemd_dir, "totally-unrelated-other-app.service"))


def test_verify_no_residue_standard_mode_treats_preserved_data_as_ok(tmp_path, monkeypatch):
    """verify_no_residue(purge=False) must NOT flag renewal-config/shadow-monitor-state as
    residue after a standard uninstall -- those are deliberately preserved, not leftover."""
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    un.standard_uninstall(install_dir)
    result = un.verify_no_residue(install_dir, purge=False)
    assert result["ok"] is True, result["problems"]


def test_verify_no_residue_standard_mode_still_flags_leftover_code(tmp_path, monkeypatch):
    _patch_systemctl_noop(monkeypatch)
    install_dir = _make_install_dir(tmp_path)
    # Don't actually uninstall -- lib/ is still present -- so this must report not-ok.
    result = un.verify_no_residue(install_dir, purge=False)
    assert result["ok"] is False
    assert any("lib" in p for p in result["problems"])
