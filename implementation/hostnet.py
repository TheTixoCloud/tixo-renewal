"""Persistent physical-host network traffic accounting -- entirely separate from
Virtualizor VPS bandwidth statistics (inventory.py/analytics.py). Reads ONLY local kernel
interface counters (/sys/class/net/<iface>/statistics/{rx,tx}_bytes); never calls the
Virtualizor API, never influences renewal eligibility, never writes anywhere outside its
own SQLite database.

TERMINOLOGY (explicit, per the real-world confusion this is designed to avoid): RX/TX here
are measured from THIS HOST's network interface, in the direction the kernel reports them --
RX is traffic arriving at the host, TX is traffic leaving it. This is NOT automatically
"customer download" or "customer upload" -- a hypervisor's physical NIC carries traffic for
every VM in both directions simultaneously, and which VM-level direction a given host-level
RX/TX byte corresponds to depends on routing/NAT details this module does not inspect. Always
label these as host-level RX/TX, never as customer upload/download without further evidence.

ACCURACY LIMITATION: traffic between the last persisted sample and a reboot or interface
reset is NOT observable and is NEVER invented. Every function here either reports a real
observed delta or explicitly marks the gap -- never silently guesses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "hostnet.sqlite3"
SCHEMA_VERSION = 1

DISCONTINUITY_REBOOT = "REBOOT"
DISCONTINUITY_ROLLOVER = "ROLLOVER"
DISCONTINUITY_UNKNOWN = "UNKNOWN"

# A 32-bit counter wrapping at ~4GiB is the classic rollover case on some older/virtual NIC
# drivers; a 64-bit counter wrapping is practically never observed. Treat a prior reading
# above this threshold, followed by a smaller one at the SAME boot, as a plausible rollover
# rather than an unexplained reset -- still just a heuristic, documented as such.
ROLLOVER_THRESHOLD_BYTES = 2**32 - 2**20  # just under the 32-bit wrap point

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS hostnet_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS hostnet_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interface TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    rx_bytes INTEGER NOT NULL,
    tx_bytes INTEGER NOT NULL,
    rx_delta INTEGER,
    tx_delta INTEGER,
    gap_seconds REAL,
    discontinuity_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_hostnet_samples_iface_time ON hostnet_samples(interface, sampled_at);

CREATE TABLE IF NOT EXISTS hostnet_cumulative (
    interface TEXT PRIMARY KEY,
    cumulative_rx_bytes INTEGER NOT NULL DEFAULT 0,
    cumulative_tx_bytes INTEGER NOT NULL DEFAULT 0,
    first_sample_at TEXT,
    last_sample_at TEXT,
    last_boot_id TEXT,
    last_rx_bytes INTEGER,
    last_tx_bytes INTEGER
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unavailable"


def open_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM hostnet_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO hostnet_meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    return conn


def record_sample(conn: sqlite3.Connection, interface: str, rx_bytes: int, tx_bytes: int,
                   boot_id: str = None, sampled_at: str = None) -> dict:
    """Records one counter reading, computing a delta against the previous sample for the
    SAME interface and classifying any discontinuity. Never computes a negative delta --
    a counter decrease within the same boot is classified ROLLOVER (if the prior value was
    near a 32-bit wrap point) or UNKNOWN (otherwise), and contributes nothing to the
    cumulative total for that sample (never invents missing traffic by guessing). A
    different boot_id than the prior sample is classified REBOOT, and credits the current
    raw reading itself as the delta (the kernel counter legitimately starts at 0 at boot, so
    the current value IS the real total since boot -- not a guess)."""
    boot_id = boot_id or get_boot_id()
    sampled_at = sampled_at or utcnow_iso()

    conn.execute("BEGIN")
    try:
        prev = conn.execute(
            "SELECT cumulative_rx_bytes, cumulative_tx_bytes, last_sample_at, last_boot_id, "
            "last_rx_bytes, last_tx_bytes FROM hostnet_cumulative WHERE interface=?",
            (interface,),
        ).fetchone()

        rx_delta = tx_delta = None
        discontinuity = None
        gap_seconds = None

        if prev is None:
            cum_rx, cum_tx = 0, 0
            first_sample_at = sampled_at
        else:
            cum_rx, cum_tx, last_sample_at, last_boot_id, last_rx, last_tx = prev
            first_sample_at = None  # unchanged, preserved by the UPSERT below
            try:
                gap_seconds = (datetime.fromisoformat(sampled_at) -
                                datetime.fromisoformat(last_sample_at)).total_seconds()
            except Exception:
                gap_seconds = None

            if boot_id != last_boot_id:
                discontinuity = DISCONTINUITY_REBOOT
                rx_delta, tx_delta = rx_bytes, tx_bytes  # counter restarted at 0 at this boot
                cum_rx += rx_delta
                cum_tx += tx_delta
            elif rx_bytes < last_rx or tx_bytes < last_tx:
                discontinuity = (DISCONTINUITY_ROLLOVER if last_rx >= ROLLOVER_THRESHOLD_BYTES
                                  else DISCONTINUITY_UNKNOWN)
                rx_delta = tx_delta = None  # never compute/guess a negative-turned-positive delta
            else:
                rx_delta = rx_bytes - last_rx
                tx_delta = tx_bytes - last_tx
                cum_rx += rx_delta
                cum_tx += tx_delta

        conn.execute(
            "INSERT INTO hostnet_samples (interface, sampled_at, boot_id, rx_bytes, tx_bytes, "
            "rx_delta, tx_delta, gap_seconds, discontinuity_type) VALUES (?,?,?,?,?,?,?,?,?)",
            (interface, sampled_at, boot_id, rx_bytes, tx_bytes, rx_delta, tx_delta,
             gap_seconds, discontinuity),
        )

        if prev is None:
            conn.execute(
                "INSERT INTO hostnet_cumulative (interface, cumulative_rx_bytes, cumulative_tx_bytes, "
                "first_sample_at, last_sample_at, last_boot_id, last_rx_bytes, last_tx_bytes) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (interface, cum_rx, cum_tx, first_sample_at, sampled_at, boot_id, rx_bytes, tx_bytes),
            )
        else:
            conn.execute(
                "UPDATE hostnet_cumulative SET cumulative_rx_bytes=?, cumulative_tx_bytes=?, "
                "last_sample_at=?, last_boot_id=?, last_rx_bytes=?, last_tx_bytes=? WHERE interface=?",
                (cum_rx, cum_tx, sampled_at, boot_id, rx_bytes, tx_bytes, interface),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"interface": interface, "sampled_at": sampled_at, "rx_delta": rx_delta,
            "tx_delta": tx_delta, "discontinuity_type": discontinuity, "gap_seconds": gap_seconds,
            "cumulative_rx_bytes": cum_rx, "cumulative_tx_bytes": cum_tx}


def get_cumulative(conn: sqlite3.Connection, interface: str):
    row = conn.execute(
        "SELECT cumulative_rx_bytes, cumulative_tx_bytes, first_sample_at, last_sample_at "
        "FROM hostnet_cumulative WHERE interface=?", (interface,),
    ).fetchone()
    if row is None:
        return None
    return {"cumulative_rx_bytes": row[0], "cumulative_tx_bytes": row[1],
            "first_sample_at": row[2], "last_sample_at": row[3]}


def get_recent_samples(conn: sqlite3.Connection, interface: str, limit: int = 2) -> list[dict]:
    rows = conn.execute(
        "SELECT sampled_at, rx_bytes, tx_bytes, rx_delta, tx_delta, discontinuity_type, gap_seconds "
        "FROM hostnet_samples WHERE interface=? ORDER BY id DESC LIMIT ?", (interface, limit),
    ).fetchall()
    return [{"sampled_at": r[0], "rx_bytes": r[1], "tx_bytes": r[2], "rx_delta": r[3],
             "tx_delta": r[4], "discontinuity_type": r[5], "gap_seconds": r[6]} for r in reversed(rows)]


def compute_live_throughput(conn: sqlite3.Connection, interface: str):
    """Instantaneous RX/TX rate (bytes/sec) from the two most recent samples -- never
    conflated with cumulative totals. Returns None if fewer than 2 samples exist, or if the
    most recent pair spans a discontinuity (a rate computed across a reboot/reset would be
    meaningless)."""
    samples = get_recent_samples(conn, interface, limit=2)
    if len(samples) < 2:
        return None
    newest = samples[-1]
    if newest["discontinuity_type"] is not None or newest["rx_delta"] is None:
        return None
    gap = newest.get("gap_seconds")
    if not gap or gap <= 0:
        return None
    return {"rx_bytes_per_sec": newest["rx_delta"] / gap, "tx_bytes_per_sec": newest["tx_delta"] / gap}


def get_samples_in_range(conn: sqlite3.Connection, interface: str, start_iso: str, end_iso: str) -> list[dict]:
    rows = conn.execute(
        "SELECT sampled_at, rx_delta, tx_delta, discontinuity_type FROM hostnet_samples "
        "WHERE interface=? AND sampled_at >= ? AND sampled_at < ? ORDER BY sampled_at",
        (interface, start_iso, end_iso),
    ).fetchall()
    return [{"sampled_at": r[0], "rx_delta": r[1], "tx_delta": r[2], "discontinuity_type": r[3]}
            for r in rows]


def last_n_days_totals(conn: sqlite3.Connection, interface: str, days: int, now: datetime = None) -> dict:
    """A rolling window (e.g. 'last 24 hours', 'last 7 days') -- calendar-timezone-agnostic
    by design, since a rolling window means the same thing regardless of which calendar an
    operator has configured for display."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    samples = get_samples_in_range(conn, interface, start.isoformat(), now.isoformat())
    totals = sum_deltas(samples)
    totals["range_start"] = start.isoformat()
    totals["range_end"] = now.isoformat()
    return totals


def current_calendar_period_totals(conn: sqlite3.Connection, interface: str, period_start_utc: str,
                                    now: datetime = None) -> dict:
    """Totals for "the current period so far" (e.g. current day or current month), given an
    already-computed period start (UTC ISO) -- calendar-boundary math (Gregorian vs Jalali)
    is the CALLER's job (see jalali.jalali_month_start / menu.py), kept separate from this
    module's pure accounting logic. Always marked complete=False for the current, still-
    in-progress period -- never confused with a fully-elapsed prior period."""
    now = now or datetime.now(timezone.utc)
    samples = get_samples_in_range(conn, interface, period_start_utc, now.isoformat())
    totals = sum_deltas(samples)
    totals["period_start"] = period_start_utc
    totals["period_end"] = now.isoformat()
    totals["complete"] = False
    return totals


def sum_deltas(samples: list[dict]) -> dict:
    """Sums only samples with a real (non-None) delta -- discontinuity samples contribute
    nothing, which is the correct, honest behavior (never invent the missing span)."""
    rx = sum(s["rx_delta"] for s in samples if s["rx_delta"] is not None)
    tx = sum(s["tx_delta"] for s in samples if s["tx_delta"] is not None)
    counted = sum(1 for s in samples if s["rx_delta"] is not None)
    return {"rx_bytes": rx, "tx_bytes": tx, "combined_bytes": rx + tx,
            "sample_count": len(samples), "counted_sample_count": counted,
            "partial": counted < len(samples)}
