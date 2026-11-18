"""Backup and restore for the local SQLite ledger(s). Uses SQLite's own online backup API
(`Connection.backup()`), which is safe to run against a live, WAL-mode database -- it takes a
transactionally-consistent snapshot without blocking or corrupting concurrent writers. Never
touches production; operates purely on local `.sqlite3` files under shadow-monitor-state/.

v1.8.0 Part 12 additions: a full, archived, optionally-encrypted backup covering every known
local database PLUS a deliberately-ALLOWLISTED (never blocklisted) set of non-secret config
files -- credentials/ (Virtualizor API creds), telegram/secrets.json (bot token/proxy
password), web/users.json (password hashes), and web/csrf_secret.key are NEVER included,
full stop, regardless of future config additions (an allowlist can only omit something new
by mistake, never leak it). Encryption uses the system `gpg` binary (symmetric AES256, found
on essentially every Linux distribution) via subprocess with the passphrase piped over
stdin -- NEVER as a command-line argument (visible in `ps`) and NEVER hand-rolled
cryptography, which this project will not attempt to implement itself."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Explicit ALLOWLIST -- a file is included ONLY if its exact relative path appears here.
# Anything not listed (credentials/, telegram/secrets.json, web/users.json,
# web/csrf_secret.key, and anything added in the future that isn't explicitly added here
# too) is never read, never archived, never sent anywhere.
ALLOWLISTED_CONFIG_FILES = (
    "policy.json", "nodes.json", "display-settings.json", "hostnet-settings.json",
    "emergency-policy-settings.json", os.path.join("telegram", "settings.json"),
    os.path.join("web", "settings.json"),
)

KNOWN_STATE_DATABASES = ("self.sqlite3", "inventory.sqlite3", "hostnet.sqlite3",
                          "emergency_policy.sqlite3")


class BackupEncryptionUnavailable(Exception):
    pass


def backup_db(db_path: str, backup_root: str, label: str = None) -> str:
    """Creates a consistent snapshot of db_path under backup_root/<timestamp>-<label>.sqlite3.
    Returns the backup file path. Safe to call while the source DB is open elsewhere (WAL)."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"no database at {db_path} to back up")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{ts}-{label}.sqlite3" if label else f"{ts}.sqlite3"
    Path(backup_root).mkdir(parents=True, exist_ok=True)
    dest = os.path.join(backup_root, name)
    src_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    return dest


def list_backups(backup_root: str) -> list[str]:
    if not os.path.isdir(backup_root):
        return []
    return sorted(f for f in os.listdir(backup_root) if f.endswith(".sqlite3"))


def restore_db(backup_path: str, target_db_path: str) -> None:
    """Restores a backup over the live database file. The CURRENT state is itself backed up
    first (to `<target>.pre-restore.sqlite3`) so a bad rollback choice is never unrecoverable."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"no backup at {backup_path}")
    if os.path.exists(target_db_path):
        safety_copy = target_db_path + ".pre-restore.sqlite3"
        shutil.copy2(target_db_path, safety_copy)
    # Verify the backup file is a readable, valid SQLite database before overwriting anything.
    check_conn = sqlite3.connect(backup_path)
    try:
        check_conn.execute("SELECT name FROM sqlite_master LIMIT 1")
    finally:
        check_conn.close()
    tmp_target = target_db_path + ".restoring.tmp"
    shutil.copy2(backup_path, tmp_target)
    os.replace(tmp_target, target_db_path)  # atomic swap


def create_full_backup_archive(state_dir: str, config_dir: str, backup_root: str,
                                 label: str = None) -> str:
    """Builds one tar.gz containing a consistent snapshot of every known state database plus
    the ALLOWLISTED (never blocklisted) non-secret config files. Secrets (credentials/,
    telegram/secrets.json, web/users.json, web/csrf_secret.key, anything not explicitly
    allowlisted) are never read or included. Returns the archive path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{ts}-{label}.tar.gz" if label else f"{ts}.tar.gz"
    Path(backup_root).mkdir(parents=True, exist_ok=True)
    archive_path = os.path.join(backup_root, name)

    with tempfile.TemporaryDirectory() as staging:
        db_dir = os.path.join(staging, "databases")
        cfg_dir = os.path.join(staging, "config")
        os.makedirs(db_dir, exist_ok=True)
        os.makedirs(cfg_dir, exist_ok=True)

        for db_name in KNOWN_STATE_DATABASES:
            src = os.path.join(state_dir, db_name)
            if not os.path.exists(src):
                continue
            src_conn = sqlite3.connect(src)
            dest_conn = sqlite3.connect(os.path.join(db_dir, db_name))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
                src_conn.close()

        for rel_path in ALLOWLISTED_CONFIG_FILES:
            src = os.path.join(config_dir, rel_path)
            if not os.path.exists(src):
                continue
            dest = os.path.join(cfg_dir, rel_path)
            Path(os.path.dirname(dest)).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(db_dir, arcname="databases")
            tar.add(cfg_dir, arcname="config")

    return archive_path


def verify_backup_archive(archive_path: str) -> dict:
    """Validates a backup archive without touching live state: checks the tar is readable,
    then runs PRAGMA integrity_check on every bundled .sqlite3 file. Returns
    {"ok": bool, "databases": {name: "ok"|<error>}, "error": <error-or-None>}."""
    if not os.path.exists(archive_path):
        return {"ok": False, "databases": {}, "error": f"no archive at {archive_path}"}

    result = {"ok": True, "databases": {}, "error": None}
    try:
        with tempfile.TemporaryDirectory() as extract_dir:
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(extract_dir, filter="data")
            db_dir = os.path.join(extract_dir, "databases")
            if not os.path.isdir(db_dir):
                result["ok"] = False
                result["error"] = "archive has no databases/ directory"
                return result
            for db_name in sorted(os.listdir(db_dir)):
                db_path = os.path.join(db_dir, db_name)
                conn = sqlite3.connect(db_path)
                try:
                    status = conn.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    conn.close()
                result["databases"][db_name] = status
                if status != "ok":
                    result["ok"] = False
    except (tarfile.TarError, sqlite3.DatabaseError, OSError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
    return result


def list_backup_archives(backup_root: str) -> list[str]:
    if not os.path.isdir(backup_root):
        return []
    return sorted(f for f in os.listdir(backup_root)
                   if f.endswith(".tar.gz") or f.endswith(".tar.gz.gpg"))


def apply_retention(backup_root: str, keep_count: int) -> list[str]:
    """Deletes all but the `keep_count` most recent backup archives (by filename, which sorts
    chronologically due to the timestamp prefix). Returns the list of removed filenames."""
    if keep_count < 1:
        raise ValueError("keep_count must be at least 1")
    archives = list_backup_archives(backup_root)
    if len(archives) <= keep_count:
        return []
    to_remove = archives[:-keep_count]
    for name in to_remove:
        os.remove(os.path.join(backup_root, name))
    return to_remove


def _require_gpg() -> str:
    for candidate in ("gpg", "gpg2"):
        path = shutil.which(candidate)
        if path:
            return path
    raise BackupEncryptionUnavailable(
        "neither gpg nor gpg2 is available on this host -- refusing to silently produce an "
        "unencrypted backup when encryption was requested")


def encrypt_backup_file(path: str, passphrase: str) -> str:
    """Symmetrically encrypts a backup archive with gpg (AES256). The passphrase is piped over
    stdin via --passphrase-fd 0, never passed as an argv string (which `ps` could reveal).
    Returns the path to the new `<path>.gpg` file; the plaintext input file is left untouched
    for the caller to remove once satisfied."""
    gpg_bin = _require_gpg()
    dest = path + ".gpg"
    proc = subprocess.run(
        [gpg_bin, "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256",
         "--passphrase-fd", "0", "--output", dest, path],
        input=passphrase.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError(f"gpg encryption failed: {proc.stderr.decode('utf-8', 'replace')}")
    return dest


def decrypt_backup_file(encrypted_path: str, passphrase: str, output_path: str) -> str:
    """Decrypts a gpg-encrypted backup archive. Passphrase is piped over stdin, never argv."""
    gpg_bin = _require_gpg()
    proc = subprocess.run(
        [gpg_bin, "--batch", "--yes", "--decrypt", "--passphrase-fd", "0",
         "--output", output_path, encrypted_path],
        input=passphrase.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"gpg decryption failed: {proc.stderr.decode('utf-8', 'replace')}")
    return output_path


def restore_full_backup_archive(archive_path: str, staging_dir: str) -> dict:
    """Extracts a backup archive to `staging_dir` ONLY -- never overwrites live state_dir or
    config_dir files. Returns {"databases": [...], "config": [...], "staging_dir": ...} listing
    what was extracted, so an operator (or a future, explicitly separate apply step) can review
    and copy files into place deliberately. This function alone can never touch production."""
    verification = verify_backup_archive(archive_path)
    if not verification["ok"]:
        raise ValueError(f"refusing to restore a failed-integrity-check archive: {verification}")
    Path(staging_dir).mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(staging_dir, filter="data")
    db_dir = os.path.join(staging_dir, "databases")
    cfg_dir = os.path.join(staging_dir, "config")
    databases = sorted(os.listdir(db_dir)) if os.path.isdir(db_dir) else []
    config_files = []
    if os.path.isdir(cfg_dir):
        for root, _dirs, files in os.walk(cfg_dir):
            for f in files:
                config_files.append(os.path.relpath(os.path.join(root, f), cfg_dir))
    return {"databases": databases, "config": sorted(config_files), "staging_dir": staging_dir}


class BackupDeliverySecurityError(Exception):
    """Raised instead of ever sending a backup to Telegram when a security requirement
    (encryption, size, transport capability) is not satisfied -- never silently degrades
    to an insecure send."""
    pass


def send_backup_to_telegram(transport, admin_ids: list, archive_path: str,
                             require_encrypted: bool = True,
                             max_size_bytes: int = 50 * 1024 * 1024) -> dict:
    """Delivers a backup archive to every admin via the given Telegram transport's
    `send_document`. SECURITY REQUIREMENTS, enforced here rather than left to the caller:
      - `archive_path` MUST end in `.gpg` (i.e. already encrypted by encrypt_backup_file())
        unless `require_encrypted=False` is explicitly passed -- never sends a plaintext
        database/config bundle to Telegram by default.
      - size is checked against `max_size_bytes` BEFORE any upload attempt.
      - a transport that does not support `send_document` (SOCKS5/RELAY -- see
        telegram_transport.py's documented limitations) raises a clear
        BackupDeliverySecurityError rather than silently failing per-recipient.
    Returns {admin_id: {"ok": bool, "error": str|None}} -- one delivery attempt per admin,
    a failure for one admin never blocks delivery to the others."""
    if require_encrypted and not archive_path.endswith(".gpg"):
        raise BackupDeliverySecurityError(
            f"{archive_path} is not an encrypted (.gpg) archive -- refusing to send a "
            f"plaintext backup to Telegram. Pass require_encrypted=False to override "
            f"(not recommended).")

    size = os.path.getsize(archive_path)
    if size > max_size_bytes:
        raise BackupDeliverySecurityError(
            f"{archive_path} is {size} bytes, exceeds max_size_bytes={max_size_bytes} -- "
            f"refusing to attempt delivery")

    if not hasattr(transport, "send_document"):
        raise BackupDeliverySecurityError(
            f"{type(transport).__name__} does not support sending documents")

    results = {}
    for admin_id in admin_ids:
        try:
            transport.send_document(admin_id, archive_path,
                                      caption=f"Backup: {os.path.basename(archive_path)}")
            results[admin_id] = {"ok": True, "error": None}
        except NotImplementedError as e:
            results[admin_id] = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 -- one admin's delivery failure must never
            results[admin_id] = {"ok": False, "error": str(e)}  # block the others
    return results
