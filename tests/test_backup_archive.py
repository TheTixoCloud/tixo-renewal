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


# ---------- send_backup_to_telegram -------------------------------------------------------------

class _FakeTransportWithDocument:
    def __init__(self, fail_for=None):
        self.sent = []
        self.fail_for = fail_for or set()

    def send_document(self, chat_id, file_path, caption=None):
        if chat_id in self.fail_for:
            raise Exception(f"simulated failure for {chat_id}")
        self.sent.append((chat_id, file_path, caption))


class _FakeTransportWithoutDocument:
    def call(self, method, params=None):
        return {"ok": True}


def test_send_backup_to_telegram_requires_encrypted_archive(tmp_path):
    plain = tmp_path / "backup.tar.gz"
    plain.write_bytes(b"plaintext data")
    transport = _FakeTransportWithDocument()
    with pytest.raises(backup.BackupDeliverySecurityError, match="not an encrypted"):
        backup.send_backup_to_telegram(transport, [1, 2], str(plain))


def test_send_backup_to_telegram_enforces_size_limit(tmp_path):
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"x" * 1000)
    transport = _FakeTransportWithDocument()
    with pytest.raises(backup.BackupDeliverySecurityError, match="exceeds max_size_bytes"):
        backup.send_backup_to_telegram(transport, [1], str(encrypted), max_size_bytes=500)


def test_send_backup_to_telegram_rejects_transport_without_send_document(tmp_path):
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")
    transport = _FakeTransportWithoutDocument()
    with pytest.raises(backup.BackupDeliverySecurityError, match="does not support"):
        backup.send_backup_to_telegram(transport, [1], str(encrypted))


def test_send_backup_to_telegram_delivers_to_all_admins(tmp_path):
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")
    transport = _FakeTransportWithDocument()
    result = backup.send_backup_to_telegram(transport, [1, 2, 3], str(encrypted))
    assert all(r["ok"] for r in result.values())
    assert len(transport.sent) == 3


def test_send_backup_to_telegram_one_failure_does_not_block_others(tmp_path):
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")
    transport = _FakeTransportWithDocument(fail_for={2})
    result = backup.send_backup_to_telegram(transport, [1, 2, 3], str(encrypted),
                                               max_retries=0, sleep_fn=lambda s: None)
    assert result[1]["ok"] is True
    assert result[2]["ok"] is False
    assert result[3]["ok"] is True


def test_send_backup_to_telegram_allows_plaintext_when_explicitly_overridden(tmp_path):
    plain = tmp_path / "backup.tar.gz"
    plain.write_bytes(b"data")
    transport = _FakeTransportWithDocument()
    result = backup.send_backup_to_telegram(transport, [1], str(plain), require_encrypted=False)
    assert result[1]["ok"] is True


# ---------- bounded retries + duplicate-delivery avoidance ---------------------------------------

class _FlakyTransport:
    """Fails a configurable number of times per admin before succeeding -- simulates a
    transient network/proxy/relay failure that a bounded retry should recover from."""
    def __init__(self, fail_count_by_admin: dict):
        self.fail_count_by_admin = dict(fail_count_by_admin)
        self.attempts = {}

    def send_document(self, chat_id, file_path, caption=None):
        self.attempts[chat_id] = self.attempts.get(chat_id, 0) + 1
        remaining = self.fail_count_by_admin.get(chat_id, 0)
        if remaining > 0:
            self.fail_count_by_admin[chat_id] = remaining - 1
            raise Exception(f"transient failure for {chat_id}")


def test_send_backup_to_telegram_retries_transient_failure_until_success(tmp_path):
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")
    transport = _FlakyTransport({1: 2})  # fails twice, succeeds on the 3rd attempt
    sleeps = []
    result = backup.send_backup_to_telegram(transport, [1], str(encrypted),
                                               max_retries=2, sleep_fn=sleeps.append)
    assert result[1]["ok"] is True
    assert transport.attempts[1] == 3
    assert len(sleeps) == 2  # one sleep between each of the two failed attempts


def test_send_backup_to_telegram_gives_up_after_max_retries(tmp_path):
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")
    transport = _FlakyTransport({1: 10})  # always fails
    result = backup.send_backup_to_telegram(transport, [1], str(encrypted),
                                               max_retries=2, sleep_fn=lambda s: None)
    assert result[1]["ok"] is False
    assert transport.attempts[1] == 3  # initial attempt + 2 retries, never more


def test_send_backup_to_telegram_never_retries_unsupported_mode_error(tmp_path):
    """NotImplementedError (e.g. an unsupported transport mode) is never transient --
    retrying it would just waste time before failing the same way again."""
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")

    class _AlwaysNotImplemented:
        def __init__(self):
            self.attempts = 0
        def send_document(self, chat_id, file_path, caption=None):
            self.attempts += 1
            raise NotImplementedError("mode not supported")

    transport = _AlwaysNotImplemented()
    result = backup.send_backup_to_telegram(transport, [1], str(encrypted),
                                               max_retries=2, sleep_fn=lambda s: None)
    assert result[1]["ok"] is False
    assert transport.attempts == 1


def test_send_backup_to_telegram_skips_already_delivered_admin_on_retry(tmp_path):
    """Re-running delivery for the SAME archive must never re-send to an admin who already
    received it successfully -- the dedupe marker persists across calls."""
    encrypted = tmp_path / "backup.tar.gz.gpg"
    encrypted.write_bytes(b"data")
    transport = _FakeTransportWithDocument()

    first = backup.send_backup_to_telegram(transport, [1, 2], str(encrypted))
    assert all(r["ok"] for r in first.values())
    assert len(transport.sent) == 2

    second = backup.send_backup_to_telegram(transport, [1, 2, 3], str(encrypted))
    assert second[1]["skipped_duplicate"] is True
    assert second[2]["skipped_duplicate"] is True
    assert second[3]["skipped_duplicate"] is False
    # Only admin 3 (never successfully delivered before) triggers a new send.
    assert len(transport.sent) == 3


def test_send_backup_to_telegram_dedupe_marker_does_not_affect_other_archives(tmp_path):
    encrypted_a = tmp_path / "a.tar.gz.gpg"
    encrypted_a.write_bytes(b"data-a")
    encrypted_b = tmp_path / "b.tar.gz.gpg"
    encrypted_b.write_bytes(b"data-b")
    transport = _FakeTransportWithDocument()

    backup.send_backup_to_telegram(transport, [1], str(encrypted_a))
    result_b = backup.send_backup_to_telegram(transport, [1], str(encrypted_b))
    assert result_b[1]["skipped_duplicate"] is False
    assert result_b[1]["ok"] is True


# ---------- renewal_cli `backup-scheduled-run` command ------------------------------------------

def test_cmd_backup_scheduled_run_blocked_when_disabled(tmp_path, capsys):
    import renewal_cli
    _configure_cli_dirs(renewal_cli, tmp_path)

    class _Args:
        pass
    rc = renewal_cli.cmd_backup_scheduled_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_backup_scheduled_run_unencrypted_succeeds(tmp_path, capsys):
    import renewal_cli
    import backup_config as bcfg
    _configure_cli_dirs(renewal_cli, tmp_path)
    bcfg.save_backup_schedule_settings(renewal_cli.CONFIG_DIR,
                                         bcfg.BackupScheduleSettings(enabled=True))

    class _Args:
        pass
    rc = renewal_cli.cmd_backup_scheduled_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Scheduled backup created" in out
    archives = backup.list_backup_archives(renewal_cli.BACKUP_DIR)
    assert len(archives) == 1


def test_cmd_backup_scheduled_run_encrypted_without_passphrase_fails(tmp_path, capsys):
    import renewal_cli
    import backup_config as bcfg
    _configure_cli_dirs(renewal_cli, tmp_path)
    bcfg.save_backup_schedule_settings(renewal_cli.CONFIG_DIR,
                                         bcfg.BackupScheduleSettings(enabled=True, encrypt=True))

    class _Args:
        pass
    rc = renewal_cli.cmd_backup_scheduled_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out


def test_cmd_backup_scheduled_run_encrypted_with_passphrase_succeeds(tmp_path, capsys):
    import renewal_cli
    import backup_config as bcfg
    _configure_cli_dirs(renewal_cli, tmp_path)
    bcfg.save_backup_schedule_settings(renewal_cli.CONFIG_DIR,
                                         bcfg.BackupScheduleSettings(enabled=True, encrypt=True))
    bcfg.store_backup_passphrase(bcfg.default_passphrase_path(renewal_cli.CONFIG_DIR), "shh")

    class _Args:
        pass
    rc = renewal_cli.cmd_backup_scheduled_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    archives = backup.list_backup_archives(renewal_cli.BACKUP_DIR)
    assert len(archives) == 1
    assert archives[0].endswith(".gpg")


def test_cmd_backup_scheduled_run_records_audit_event(tmp_path, capsys):
    import renewal_cli
    import backup_config as bcfg
    import audit_log
    _configure_cli_dirs(renewal_cli, tmp_path)
    bcfg.save_backup_schedule_settings(renewal_cli.CONFIG_DIR,
                                         bcfg.BackupScheduleSettings(enabled=True))

    class _Args:
        pass
    renewal_cli.cmd_backup_scheduled_run(_Args())
    capsys.readouterr()
    conn = audit_log.open_db(os.path.join(renewal_cli.STATE_DIR, "audit_log.sqlite3"))
    events = audit_log.list_recent_events(conn)
    conn.close()
    assert any(e["action"] == "backup_full_created" for e in events)


def test_cmd_backup_scheduled_run_pushes_alert_on_integrity_failure(tmp_path, capsys, monkeypatch):
    """v1.10.11: the scheduled backup is the one automatic path with nobody interactively
    watching it -- an integrity failure must be PUSHED (reusing readiness_notifier's same
    best-effort Telegram transport), not just printed to a log an operator would need to
    go looking for."""
    import renewal_cli
    import backup_config as bcfg
    import readiness_notifier as rn
    _configure_cli_dirs(renewal_cli, tmp_path)
    bcfg.save_backup_schedule_settings(renewal_cli.CONFIG_DIR,
                                         bcfg.BackupScheduleSettings(enabled=True))
    monkeypatch.setattr(backup, "verify_backup_archive",
                         lambda archive: {"ok": False, "error": "corrupt tar"})
    pushed = []
    monkeypatch.setattr(rn, "send_telegram_alert", lambda config_dir, text: pushed.append(text))

    class _Args:
        pass
    renewal_cli.cmd_backup_scheduled_run(_Args())
    out = capsys.readouterr().out
    assert "WARNING: backup archive failed integrity verification" in out
    assert len(pushed) == 1
    assert "BACKUP INTEGRITY FAILED" in pushed[0]


def test_cmd_backup_scheduled_run_telegram_delivery_skipped_when_bot_disabled(tmp_path, capsys):
    import renewal_cli
    import backup_config as bcfg
    _configure_cli_dirs(renewal_cli, tmp_path)
    bcfg.save_backup_schedule_settings(
        renewal_cli.CONFIG_DIR,
        bcfg.BackupScheduleSettings(enabled=True, encrypt=True, telegram_delivery_enabled=True))
    bcfg.store_backup_passphrase(bcfg.default_passphrase_path(renewal_cli.CONFIG_DIR), "shh")

    class _Args:
        pass
    rc = renewal_cli.cmd_backup_scheduled_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Telegram delivery skipped" in out
