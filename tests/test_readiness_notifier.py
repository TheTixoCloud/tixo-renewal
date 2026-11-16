"""Tests for the read-only readiness notifier. Never touches Virtualizor, never polls, never
writes to the renewal operations ledger/policy.json/nodes.json -- only reads an existing
shadow-monitor SQLite file and writes its own separate dedup-state/alert-log files.
"""
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import shadow_monitor as sm
import readiness_notifier as notifier


def iso(dt):
    return dt.isoformat()


def row(vpsid, created_epoch, quota, used):
    return {"vpsid": vpsid, "created_epoch": created_epoch,
            "quota_gb": Decimal(str(quota)), "used_gb": Decimal(str(used))}


def make_plausible_db(tmp_path, vpsid=8):
    db_path = str(tmp_path / "node.sqlite3")
    conn = sm.open_db(db_path)
    created_epoch = int(datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp())
    t1 = datetime(2026, 10, 15, 9, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc)
    sm.ingest_poll(conn, [row(vpsid, created_epoch, 1000, 900)], iso(t1))
    sm.ingest_poll(conn, [row(vpsid, created_epoch, 1000, 1)], iso(t2))
    conn.close()
    return db_path


def make_policy_config(tmp_path):
    path = str(tmp_path / "policy.json")
    import policy as pol
    pol.write_default_policy_config(path)
    return path


def test_no_candidates_yet_produces_no_alert(tmp_path):
    db_path = str(tmp_path / "node.sqlite3")
    conn = sm.open_db(db_path)
    conn.close()
    state_path = str(tmp_path / "notified.json")
    log_path = str(tmp_path / "alerts.log")
    rc = notifier.run(db_path, "self", make_policy_config(tmp_path), state_path, log_path)
    assert rc == 0
    assert not os.path.exists(log_path)


def test_plausible_candidate_triggers_one_alert_with_pilot_plan(tmp_path, capsys):
    db_path = make_plausible_db(tmp_path)
    state_path = str(tmp_path / "notified.json")
    log_path = str(tmp_path / "alerts.log")
    rc = notifier.run(db_path, "self", make_policy_config(tmp_path), state_path, log_path)
    assert rc == 0
    assert os.path.exists(log_path)
    content = open(log_path).read()
    assert "vpsid=8" in content
    assert "PROPOSED SAFE SINGLE-VPS PILOT PLAN" in content
    assert "WRITE_APPROVED" in content


def test_duplicate_runs_never_re_notify_the_same_period(tmp_path):
    """The core requirement: must never fire twice for the same real event."""
    db_path = make_plausible_db(tmp_path)
    state_path = str(tmp_path / "notified.json")
    log_path = str(tmp_path / "alerts.log")
    policy_path = make_policy_config(tmp_path)

    notifier.run(db_path, "self", policy_path, state_path, log_path)
    first_size = os.path.getsize(log_path)

    # Run again (simulating the next timer tick) -- same underlying data, same period.
    notifier.run(db_path, "self", policy_path, state_path, log_path)
    second_size = os.path.getsize(log_path)
    assert second_size == first_size  # nothing new appended


def test_notified_state_persists_across_process_restarts(tmp_path):
    """Must survive a fresh process (e.g. after an SSH disconnect / server restart) reading
    the same on-disk state file, not just in-memory dedup within one run."""
    db_path = make_plausible_db(tmp_path)
    state_path = str(tmp_path / "notified.json")
    log_path = str(tmp_path / "alerts.log")
    policy_path = make_policy_config(tmp_path)

    notifier.run(db_path, "self", policy_path, state_path, log_path)
    assert os.path.exists(state_path)
    saved = json.loads(open(state_path).read())
    assert len(saved["notified"]) == 1

    # Fresh call, as if a brand new process/session -- must still recognize it as notified.
    notifier.run(db_path, "self", policy_path, state_path, log_path)
    saved_again = json.loads(open(state_path).read())
    assert saved_again == saved


def test_corrupt_state_file_fails_safe_not_crash(tmp_path):
    db_path = make_plausible_db(tmp_path)
    state_path = str(tmp_path / "notified.json")
    Path(state_path).write_text("not valid json {{{")
    log_path = str(tmp_path / "alerts.log")
    rc = notifier.run(db_path, "self", make_policy_config(tmp_path), state_path, log_path)
    assert rc == 0
    assert os.path.exists(log_path)  # still alerts despite the corrupt pre-existing state


def test_never_writes_to_renewal_operations_table_or_policy_file(tmp_path):
    """Explicit safety check: the notifier must never touch policy.json or the real
    write-authorization-gated `operations` table (it legitimately reuses
    detect_renewal_candidates()'s own pre-existing renewal_checkpoints bookkeeping -- the
    same thing `status`/`dry-run` already do -- but must never create or modify an
    `operations` row, which is what an actual quota WRITE would require)."""
    import sqlite3
    db_path = make_plausible_db(tmp_path)
    policy_path = make_policy_config(tmp_path)
    policy_mtime_before = os.path.getmtime(policy_path)
    conn = sqlite3.connect(db_path)
    ops_before = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    conn.close()

    notifier.run(db_path, "self", policy_path, str(tmp_path / "notified.json"),
                 str(tmp_path / "alerts.log"))

    assert os.path.getmtime(policy_path) == policy_mtime_before
    conn = sqlite3.connect(db_path)
    ops_after = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    conn.close()
    assert ops_after == ops_before == 0
