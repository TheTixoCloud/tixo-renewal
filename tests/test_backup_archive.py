import json
import os
import sqlite3
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import backup


def _make_db(path, table="t", rows=((1, "a"),)):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id INTEGER, val TEXT)")
    conn.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def state_and_config(tmp_path):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    state_dir.mkdir()
    config_dir.mkdir()
    (config_dir / "telegram").mkdir()
    (config_dir / "web").mkdir()
    (config_dir / "credentials").mkdir()

    _make_db(str(state_dir / "inventory.sqlite3"))
    _make_db(str(state_dir / "hostnet.sqlite3"))

    (config_dir / "policy.json").write_text(json.dumps({"some": "policy"}))
    (config_dir / "nodes.json").write_text(json.dumps({"nodes": []}))
    (config_dir / "telegram" / "settings.json").write_text(json.dumps({"enabled": False}))
    (config_dir / "telegram" / "secrets.json").write_text(json.dumps({"bot_token": "SECRET"}))
    (config_dir / "web" / "users.json").write_text(json.dumps({"pwhash": "SECRET"}))
    (config_dir / "web" / "csrf_secret.key").write_text("SECRET_KEY_MATERIAL")
    (config_dir / "credentials" / "api.json").write_text(json.dumps({"key": "SECRET"}))

    return str(state_dir), str(config_dir)


def test_create_full_backup_archive_includes_known_databases(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root, label="test")
    assert os.path.exists(archive)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any("databases/inventory.sqlite3" in n for n in names)
    assert any("databases/hostnet.sqlite3" in n for n in names)
    assert any("config/policy.json" in n for n in names)
    assert any("config/nodes.json" in n for n in names)
    assert any("config/telegram/settings.json" in n for n in names)


def test_create_full_backup_archive_never_includes_secrets(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    joined = "\n".join(names)
    assert "secrets.json" not in joined
    assert "users.json" not in joined
    assert "csrf_secret.key" not in joined
    assert "credentials" not in joined
    assert "api.json" not in joined


def test_verify_backup_archive_reports_ok(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root)
    result = backup.verify_backup_archive(archive)
    assert result["ok"] is True
    assert result["databases"]["inventory.sqlite3"] == "ok"
    assert result["databases"]["hostnet.sqlite3"] == "ok"


def test_verify_backup_archive_missing_file(tmp_path):
    result = backup.verify_backup_archive(str(tmp_path / "nope.tar.gz"))
    assert result["ok"] is False
    assert "no archive" in result["error"]


def test_verify_backup_archive_corrupt_db_detected(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root)

    # Corrupt the archive's database bytes directly to simulate bit rot / truncation.
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(corrupt_dir), filter="data")
    db_path = corrupt_dir / "databases" / "inventory.sqlite3"
    with open(db_path, "r+b") as f:
        f.seek(20)
        f.write(b"\x00" * 50)

    corrupt_archive = str(tmp_path / "corrupt.tar.gz")
    with tarfile.open(corrupt_archive, "w:gz") as tar:
        tar.add(str(corrupt_dir / "databases"), arcname="databases")
        tar.add(str(corrupt_dir / "config"), arcname="config")

    result = backup.verify_backup_archive(corrupt_archive)
    assert result["ok"] is False


def test_list_backup_archives_and_retention(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archives = []
    for i in range(5):
        a = backup.create_full_backup_archive(state_dir, config_dir, backup_root, label=f"n{i}")
        archives.append(a)

    listed = backup.list_backup_archives(backup_root)
    assert len(listed) == 5

    removed = backup.apply_retention(backup_root, keep_count=2)
    assert len(removed) == 3
    remaining = backup.list_backup_archives(backup_root)
    assert len(remaining) == 2
    # The two most recent (last two created) must survive.
    assert remaining == sorted(remaining)


def test_apply_retention_rejects_invalid_keep_count(tmp_path):
    with pytest.raises(ValueError):
        backup.apply_retention(str(tmp_path), keep_count=0)


def test_encrypt_and_decrypt_backup_roundtrip(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root)

    encrypted = backup.encrypt_backup_file(archive, passphrase="correct-horse-battery-staple")
    assert os.path.exists(encrypted)
    assert encrypted.endswith(".gpg")

    with open(encrypted, "rb") as f:
        encrypted_bytes = f.read()
    with open(archive, "rb") as f:
        plaintext_bytes = f.read()
    assert encrypted_bytes != plaintext_bytes

    decrypted_path = str(tmp_path / "decrypted.tar.gz")
    backup.decrypt_backup_file(encrypted, passphrase="correct-horse-battery-staple",
                                 output_path=decrypted_path)
    with open(decrypted_path, "rb") as f:
        roundtrip_bytes = f.read()
    assert roundtrip_bytes == plaintext_bytes


def test_decrypt_with_wrong_passphrase_fails(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root)
    encrypted = backup.encrypt_backup_file(archive, passphrase="right-passphrase")

    with pytest.raises(RuntimeError):
        backup.decrypt_backup_file(encrypted, passphrase="wrong-passphrase",
                                     output_path=str(tmp_path / "out.tar.gz"))


def test_require_gpg_raises_clear_error_when_unavailable(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    with pytest.raises(backup.BackupEncryptionUnavailable):
        backup._require_gpg()


def test_restore_full_backup_archive_extracts_to_staging_only(state_and_config, tmp_path):
    state_dir, config_dir = state_and_config
    backup_root = str(tmp_path / "backups")
    archive = backup.create_full_backup_archive(state_dir, config_dir, backup_root)

    staging = str(tmp_path / "restore_staging")
    result = backup.restore_full_backup_archive(archive, staging)
    assert "inventory.sqlite3" in result["databases"]
    assert "hostnet.sqlite3" in result["databases"]
    assert "policy.json" in result["config"]
    assert result["staging_dir"] == staging

    # Live state/config directories must be completely untouched.
    assert set(os.listdir(state_dir)) == {"inventory.sqlite3", "hostnet.sqlite3"}
    live_inventory = sqlite3.connect(os.path.join(state_dir, "inventory.sqlite3"))
    assert live_inventory.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    live_inventory.close()


def test_restore_full_backup_archive_refuses_corrupt_archive(tmp_path):
    bad_archive = tmp_path / "bad.tar.gz"
    bad_archive.write_bytes(b"not a real tar.gz file at all")
    with pytest.raises(ValueError):
        backup.restore_full_backup_archive(str(bad_archive), str(tmp_path / "staging"))


# ---------- renewal_cli `backup-full` command --------------------------------------------------

def _configure_cli_dirs(renewal_cli, tmp_path):
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    renewal_cli.BACKUP_DIR = str(tmp_path / "backups")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    _make_db(os.path.join(renewal_cli.STATE_DIR, "inventory.sqlite3"))


def test_cmd_backup_full_creates_archive_and_audits(tmp_path, capsys):
    import renewal_cli
    _configure_cli_dirs(renewal_cli, tmp_path)

    class _Args:
        label = "cli-test"
        encrypt = False
        keep = None
    rc = renewal_cli.cmd_backup_full(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Backup archive created" in out

    archives = backup.list_backup_archives(renewal_cli.BACKUP_DIR)
    assert len(archives) == 1

    import audit_log
    conn = audit_log.open_db(os.path.join(renewal_cli.STATE_DIR, "audit_log.sqlite3"))
    events = audit_log.list_recent_events(conn)
    conn.close()
    assert any(e["action"] == "backup_full_created" for e in events)


def test_cmd_backup_full_encrypt_requires_passphrase_env(tmp_path, capsys, monkeypatch):
    import renewal_cli
    _configure_cli_dirs(renewal_cli, tmp_path)
    monkeypatch.delenv("TIXO_BACKUP_PASSPHRASE", raising=False)

    class _Args:
        label = None
        encrypt = True
        keep = None
    rc = renewal_cli.cmd_backup_full(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "TIXO_BACKUP_PASSPHRASE" in out


def test_cmd_backup_full_encrypt_with_passphrase_succeeds(tmp_path, capsys, monkeypatch):
    import renewal_cli
    _configure_cli_dirs(renewal_cli, tmp_path)
    monkeypatch.setenv("TIXO_BACKUP_PASSPHRASE", "test-passphrase")

    class _Args:
        label = None
        encrypt = True
        keep = None
    rc = renewal_cli.cmd_backup_full(_Args())
    assert rc == 0
    archives = backup.list_backup_archives(renewal_cli.BACKUP_DIR)
    assert len(archives) == 1
    assert archives[0].endswith(".gpg")


def test_cmd_backup_full_applies_retention(tmp_path, capsys):
    import renewal_cli
    _configure_cli_dirs(renewal_cli, tmp_path)

    class _Args:
        label = None
        encrypt = False
        keep = 1
    renewal_cli.cmd_backup_full(_Args())
    renewal_cli.cmd_backup_full(_Args())
    renewal_cli.cmd_backup_full(_Args())
    capsys.readouterr()
    archives = backup.list_backup_archives(renewal_cli.BACKUP_DIR)
    assert len(archives) == 1
