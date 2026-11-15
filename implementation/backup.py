"""Backup and restore for the local SQLite ledger(s). Uses SQLite's own online backup API
(`Connection.backup()`), which is safe to run against a live, WAL-mode database -- it takes a
transactionally-consistent snapshot without blocking or corrupting concurrent writers. Never
touches production; operates purely on local `.sqlite3` files under shadow-monitor-state/.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


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
