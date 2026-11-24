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


# ---------- v1.10.11: continuous health monitoring + bounded self-heal + Telegram push ----------

def _empty_db(tmp_path):
    db_path = str(tmp_path / "empty.sqlite3")
    sm.open_db(db_path).close()
    return db_path


def test_check_health_problems_self_heals_an_unhealthy_core_timer(monkeypatch, tmp_path):
    """The one bounded, safe self-heal this module performs: a core READ-ONLY timer found
    not enabled/active gets exactly ONE re-activation attempt via the same
    install_and_enable_units() the fresh-install path already uses -- always safe by
    construction, since none of these four timers can write to Virtualizor."""
    import installer as inst
    import service_activation as sa

    fake_status = {
        "virt-renewal-monitor.timer": {"service": "virt-renewal-monitor.service",
                                         "is_enabled": "disabled", "is_active": "inactive",
                                         "service_is_failed": "inactive"},
    }
    monkeypatch.setattr(inst, "health_check", lambda *a, **k: {
        "ok": False, "problems": ["virt-renewal-monitor.timer is not enabled"],
        "systemd": fake_status})
    calls = []
    monkeypatch.setattr(sa, "install_and_enable_units", lambda src_dir, pairs: (
        calls.append(pairs),
        {"ok": True, "copied": [], "enabled": ["virt-renewal-monitor.timer"], "problems": []})[1])

    problems, notes = notifier.check_health_problems(
        str(tmp_path / "config"), str(tmp_path / "state"), str(tmp_path / "systemd"))
    assert problems == []  # the healed problem must be removed from the persistent set
    assert any("self-healed" in n for n in notes)
    assert calls == [[("virt-renewal-monitor.service", "virt-renewal-monitor.timer")]]


def test_check_health_problems_self_heal_failure_keeps_the_problem_and_reports_it(
        monkeypatch, tmp_path):
    import installer as inst
    import service_activation as sa

    fake_status = {
        "virt-renewal-monitor.timer": {"service": "virt-renewal-monitor.service",
                                         "is_enabled": "disabled", "is_active": "inactive",
                                         "service_is_failed": "inactive"},
    }
    monkeypatch.setattr(inst, "health_check", lambda *a, **k: {
        "ok": False, "problems": ["virt-renewal-monitor.timer is not enabled"],
        "systemd": fake_status})
    monkeypatch.setattr(sa, "install_and_enable_units", lambda src_dir, pairs: {
        "ok": False, "copied": [], "enabled": [], "problems": ["permission denied"]})

    problems, notes = notifier.check_health_problems(
        str(tmp_path / "config"), str(tmp_path / "state"), str(tmp_path / "systemd"))
    assert problems == ["virt-renewal-monitor.timer is not enabled"]  # NOT silently cleared
    assert any("FAILED" in n for n in notes)


def test_check_health_problems_never_touches_the_write_capable_apply_timer(monkeypatch, tmp_path):
    """Even if the write-capable apply timer somehow appeared unhealthy, self-heal must
    never attempt to (re)activate it -- that is a financial-write-capable unit, and this
    module has nothing to do with deciding whether writes should happen."""
    import installer as inst
    import service_activation as sa

    fake_status = {
        sa.WRITE_CAPABLE_UNIT[1]: {"service": sa.WRITE_CAPABLE_UNIT[0],
                                     "is_enabled": "disabled", "is_active": "inactive",
                                     "service_is_failed": "inactive"},
    }
    monkeypatch.setattr(inst, "health_check", lambda *a, **k: {
        "ok": False, "problems": [f"{sa.WRITE_CAPABLE_UNIT[1]} is not enabled"],
        "systemd": fake_status})
    called = []
    monkeypatch.setattr(sa, "install_and_enable_units", lambda *a, **k: called.append(1))

    problems, notes = notifier.check_health_problems(
        str(tmp_path / "config"), str(tmp_path / "state"), str(tmp_path / "systemd"))
    assert called == []  # never even attempted
    assert problems == [f"{sa.WRITE_CAPABLE_UNIT[1]} is not enabled"]  # left exactly as-is


def test_check_health_problems_no_self_heal_when_disabled(monkeypatch, tmp_path):
    import installer as inst
    import service_activation as sa
    fake_status = {
        "virt-renewal-monitor.timer": {"service": "virt-renewal-monitor.service",
                                         "is_enabled": "disabled", "is_active": "inactive",
                                         "service_is_failed": "inactive"},
    }
    monkeypatch.setattr(inst, "health_check", lambda *a, **k: {
        "ok": False, "problems": ["virt-renewal-monitor.timer is not enabled"],
        "systemd": fake_status})
    called = []
    monkeypatch.setattr(sa, "install_and_enable_units", lambda *a, **k: called.append(1))

    problems, notes = notifier.check_health_problems(
        str(tmp_path / "config"), str(tmp_path / "state"), str(tmp_path / "systemd"),
        attempt_self_heal=False)
    assert called == []
    assert notes == []
    assert problems == ["virt-renewal-monitor.timer is not enabled"]


def test_run_dedups_health_problems_and_detects_recovery(monkeypatch, tmp_path):
    """Core dedup/recovery contract: a problem alerted once must never be re-alerted every
    single run while it persists, and clearing it must produce exactly one RECOVERED
    notice -- never silence (an operator who never sees 'recovered' can't tell a
    still-ongoing problem from one that quietly went away)."""
    import installer as inst
    db_path = _empty_db(tmp_path)
    policy_path = make_policy_config(tmp_path)
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    state_dir = str(tmp_path / "state")
    health_state_path = str(tmp_path / "health-notified.json")

    monkeypatch.setattr(inst, "health_check", lambda *a, **k: {
        "ok": False, "problems": ["nodes.json did not load: boom"], "systemd": {}})

    notifier.run(db_path, "self", policy_path, str(tmp_path / "notified.json"),
                 str(tmp_path / "alerts.log"), config_dir=config_dir, state_dir=state_dir,
                 health_notified_state_path=health_state_path, attempt_self_heal=False)
    out1 = open(str(tmp_path / "alerts.log")).read()
    assert "HEALTH PROBLEM" in out1
    assert out1.count("nodes.json did not load") == 1

    # Same problem, second run -- must NOT re-alert.
    notifier.run(db_path, "self", policy_path, str(tmp_path / "notified.json"),
                 str(tmp_path / "alerts.log"), config_dir=config_dir, state_dir=state_dir,
                 health_notified_state_path=health_state_path, attempt_self_heal=False)
    out2 = open(str(tmp_path / "alerts.log")).read()
    assert out2.count("HEALTH PROBLEM") == 1  # unchanged from before -- no duplicate

    # Problem clears -- must produce exactly one RECOVERED notice.
    monkeypatch.setattr(inst, "health_check", lambda *a, **k: {
        "ok": True, "problems": [], "systemd": {}})
    notifier.run(db_path, "self", policy_path, str(tmp_path / "notified.json"),
                 str(tmp_path / "alerts.log"), config_dir=config_dir, state_dir=state_dir,
                 health_notified_state_path=health_state_path, attempt_self_heal=False)
    out3 = open(str(tmp_path / "alerts.log")).read()
    assert "HEALTH RECOVERED" in out3
    assert out3.count("HEALTH RECOVERED") == 1


def test_run_without_config_dir_never_runs_health_check_unchanged_prior_behavior(
        monkeypatch, tmp_path):
    """Backward compatibility: a caller that omits config_dir/state_dir (every pre-v1.10.11
    caller) gets EXACTLY the prior readiness-only behavior -- health_check must not even be
    imported/called."""
    import installer as inst
    called = []
    monkeypatch.setattr(inst, "health_check", lambda *a, **k: called.append(1))
    db_path = _empty_db(tmp_path)
    policy_path = make_policy_config(tmp_path)
    notifier.run(db_path, "self", policy_path, str(tmp_path / "notified.json"),
                 str(tmp_path / "alerts.log"))
    assert called == []


def test_send_telegram_alert_pushes_when_configured(monkeypatch, tmp_path):
    import telegram_config as tgc
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    settings = tgc.load_telegram_settings(config_dir)
    settings.enabled = True
    settings.admin_ids = [111]
    tgc.save_telegram_settings(config_dir, settings)
    tgc.store_telegram_secrets(tgc.default_secrets_path(config_dir), bot_token="123:ABC")

    sent = []

    class FakeTransport:
        def call(self, method, params=None, http_post=None):
            sent.append((method, params))
            return {"ok": True}

    import telegram_bot as tbot
    monkeypatch.setattr(tbot, "build_transport", lambda settings, secrets: FakeTransport())

    notifier.send_telegram_alert(config_dir, "hello admins")
    assert sent == [("sendMessage", {"chat_id": 111, "text": "hello admins"})]


def test_send_telegram_alert_silent_noop_when_not_configured(tmp_path):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir)
    notifier.send_telegram_alert(config_dir, "hello")  # must not raise
