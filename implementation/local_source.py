"""Host-local, read-only Virtualizor DB access. Runs directly ON the Virtualizor host itself
-- no SSH, no dependency on the Ubuntu development server at runtime. Reuses the exact
credential-reading approach already proven safe and reviewed in `/root/virt-creation-times.py`
(read `universal.php`, connect with the panel's own DB user via the local mysql client), but
as a self-contained function bundled in this package rather than relying on that specific file
existing on the target host.

Every function here is a pure SELECT. Nothing in this module writes anything, ever.
"""
from __future__ import annotations

import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Optional

UNIVERSAL_PHP_PATH = "/usr/local/virtualizor/universal.php"
MYSQL_BIN = "/usr/local/emps/bin/mysql"
MYSQL_SOCKET = "/usr/local/emps/var/mysql/mysql.sock"


class LocalSourceError(Exception):
    pass


def _read_db_config(universal_php_path: str = UNIVERSAL_PHP_PATH) -> dict:
    text = Path(universal_php_path).read_text(errors="replace")

    def config(key):
        pattern = (r"""\$globals\s*\[\s*['"]""" + re.escape(key) +
                   r"""['"]\s*\]\s*=\s*(['"])((?:\\.|(?!\1).)*)\1\s*;""")
        m = re.search(pattern, text, re.S)
        if not m:
            raise LocalSourceError(f"could not find config key {key!r} in {universal_php_path}")
        if "\\" in m.group(2):
            raise LocalSourceError(f"escaped value for {key!r} not supported")
        return m.group(2)

    return {"host": config("dbhost"), "db": config("db"), "user": config("dbuser"),
            "password": config("dbpass")}


def _mysql_select(sql: str, db_config: dict, timeout: int = 20,
                   mysql_bin: str = MYSQL_BIN, socket: str = MYSQL_SOCKET) -> str:
    args = [mysql_bin, "--no-defaults", "--batch", "--skip-column-names", "--connect-timeout=5"]
    if db_config["host"] == "localhost":
        args += ["--protocol=socket", "--socket=socket".replace("socket", socket)]
    else:
        args += ["--host", db_config["host"]]
    args += ["--user", db_config["user"], db_config["db"], "--execute", sql]
    env = os.environ.copy()
    env["MYSQL_PWD"] = db_config["password"]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise LocalSourceError(f"mysql query failed: {result.stderr.strip()}")
    return result.stdout


def fetch_live_rows_local(universal_php_path: str = UNIVERSAL_PHP_PATH) -> list[dict]:
    """The host-local equivalent of shadow_monitor.fetch_live_rows() -- same output shape
    (vpsid, created_epoch, quota_gb, used_gb), but queries the DB directly on this host
    instead of going over SSH to a remote one."""
    db_config = _read_db_config(universal_php_path)
    sql = "SELECT vpsid, time, bandwidth, used_bandwidth FROM vps ORDER BY vpsid;"
    raw = _mysql_select(sql, db_config)
    rows = []
    for line in raw.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        vpsid, created_unix, bandwidth, used_bandwidth = parts
        rows.append({
            "vpsid": int(vpsid), "created_epoch": int(created_unix),
            "quota_gb": Decimal(bandwidth), "used_gb": Decimal(used_bandwidth),
        })
    return rows


def fetch_single_vps_quota(vpsid: int, universal_php_path: str = UNIVERSAL_PHP_PATH) -> Decimal:
    """Minimal single-row read, used by the writer for the immediately-before/-after
    verification reads around an apply -- kept separate from fetch_live_rows_local() so a
    verification read is cheap and fast, not a full-table scan."""
    db_config = _read_db_config(universal_php_path)
    sql = f"SELECT bandwidth FROM vps WHERE vpsid={int(vpsid)};"
    raw = _mysql_select(sql, db_config).strip()
    if not raw:
        raise LocalSourceError(f"vpsid {vpsid} not found")
    return Decimal(raw.splitlines()[0])


def fetch_single_vps_used(vpsid: int, universal_php_path: str = UNIVERSAL_PHP_PATH) -> Decimal:
    """Read-only read of used_bandwidth alone -- used by the gated disposable-VPS integration
    test to empirically determine whether a managevps bandwidth edit also resets usage,
    without assuming either way."""
    db_config = _read_db_config(universal_php_path)
    sql = f"SELECT used_bandwidth FROM vps WHERE vpsid={int(vpsid)};"
    raw = _mysql_select(sql, db_config).strip()
    if not raw:
        raise LocalSourceError(f"vpsid {vpsid} not found")
    return Decimal(raw.splitlines()[0])


def fetch_single_vps_all(vpsid: int, universal_php_path: str = UNIVERSAL_PHP_PATH) -> dict:
    """Single-row, read-only fetch of all identity/bandwidth fields needed for an inspect
    report. One query instead of two separate ones -- cheaper and the data is always
    from the same point in time (no race between quota and used reads)."""
    db_config = _read_db_config(universal_php_path)
    sql = f"SELECT vpsid, time, bandwidth, used_bandwidth FROM vps WHERE vpsid={int(vpsid)};"
    raw = _mysql_select(sql, db_config).strip()
    if not raw:
        raise LocalSourceError(f"vpsid {vpsid} not found")
    parts = raw.splitlines()[0].split("\t")
    if len(parts) != 4:
        raise LocalSourceError(f"unexpected query result format for vpsid {vpsid}")
    return {
        "vpsid": int(parts[0]),
        "created_epoch": int(parts[1]),
        "quota_gb": Decimal(parts[2]),
        "used_gb": Decimal(parts[3]),
    }
