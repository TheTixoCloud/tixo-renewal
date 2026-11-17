"""Tests for reports.py (v1.8.0 Part 4): report content generation (honest about missing
data, never fabricates), message chunking for Telegram's length limit, per-recipient
delivery confirmation, and idempotent once-per-day scheduling.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import reports
import telegram_config as tc


def _config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    return str(d)


def _state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return str(d)


# ---------- report generation: honest about missing data --------------------------------------

def test_generate_report_on_empty_state_does_not_crash_and_is_honest(tmp_path):
    text = reports.generate_report_text(_config_dir(tmp_path), _state_dir(tmp_path))
    assert "TixoCloud Control Center" in text
    assert "No network interface configured" in text
    assert "No inventory synced yet" in text
    assert "not yet implemented" in text.lower()  # backup section


def test_generate_report_includes_hostname_and_period_label(tmp_path):
    text = reports.generate_report_text(_config_dir(tmp_path), _state_dir(tmp_path),
                                         period_label="Weekly")
    assert "Weekly Report" in text


def test_generate_report_shows_real_inventory_data(tmp_path):
    import inventory as inv
    from decimal import Decimal
    state_dir = _state_dir(tmp_path)
    conn = inv.open_db(os.path.join(state_dir, "inventory.sqlite3"))
    parse_result = {
        "complete": True,
        "records": [{"vpsid": 1, "hostname": "h1", "status": "unknown", "quota_gb": Decimal("100"),
                      "used_gb": Decimal("50"), "ram_mb": "1024", "cores": "1", "disk_gb": "20",
                      "virtualization": "kvm", "locked": "unavailable", "node_serid": "unavailable",
                      "ip_primary": "unavailable", "ip_count": "unavailable", "ip_list": "unavailable",
                      "runtime_status": "running", "network_suspended_reason": None}],
        "skipped_count": 0, "error": None, "authoritative_total": 1,
    }
    inv.ingest_snapshot(conn, "self", parse_result)
    conn.close()
    text = reports.generate_report_text(_config_dir(tmp_path), state_dir)
    assert "Total: 1" in text
    assert "Running: 1" in text


def test_generate_report_shows_configured_network_data(tmp_path):
    import hostnet
    import hostnet_sync
    config_dir = _config_dir(tmp_path)
    state_dir = _state_dir(tmp_path)
    hostnet_sync.save_monitored_interface(config_dir, "lo", confirmed=True,
                                           get_network_interfaces=lambda: [
                                               {"name": "lo", "is_virtual": False}])
    conn = hostnet.open_db(os.path.join(state_dir, "hostnet.sqlite3"))
    hostnet.record_sample(conn, "lo", 0, 0, boot_id="b1", sampled_at="2026-01-01T00:00:00+00:00")
    hostnet.record_sample(conn, "lo", 5_000_000_000, 2_000_000_000, boot_id="b1",
                           sampled_at="2026-01-01T00:05:00+00:00")
    conn.close()
    text = reports.generate_report_text(config_dir, state_dir)
    assert "Monitored interface: lo" in text
    assert "5.00 GB" in text
    assert "Raw kernel counter" in text
    assert "NOT added to persistent recorded totals" in text


def test_generate_report_never_fabricates_vps_restart_events(tmp_path):
    """The mission explicitly forbids inferring restarts from a running status -- confirm
    this module never even mentions restarts/reboots in VPS context."""
    text = reports.generate_report_text(_config_dir(tmp_path), _state_dir(tmp_path))
    assert "restart" not in text.lower()
    assert "reboot" not in text.lower()


# ---------- chunking --------------------------------------------------------------------------

def test_chunk_short_text_returns_single_chunk():
    assert reports.chunk_telegram_message("short text") == ["short text"]


def test_chunk_long_text_splits_on_line_boundaries():
    lines = ["line " + str(i) * 100 for i in range(100)]
    text = "\n".join(lines)
    chunks = reports.chunk_telegram_message(text, max_len=500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500
    # rejoining reconstructs the original content losslessly
    assert "\n".join(chunks) == text


def test_chunk_single_line_longer_than_limit_hard_splits():
    text = "x" * 10000
    chunks = reports.chunk_telegram_message(text, max_len=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


# ---------- delivery: per-recipient confirmation -----------------------------------------------

class _FakeTransport:
    def __init__(self, ok_chat_ids=None, raise_for_chat_ids=None):
        self.ok_chat_ids = ok_chat_ids if ok_chat_ids is not None else "all"
        self.raise_for_chat_ids = raise_for_chat_ids or set()
        self.sent = []

    def call(self, method, params=None):
        chat_id = params["chat_id"]
        self.sent.append((chat_id, params["text"]))
        if chat_id in self.raise_for_chat_ids:
            import telegram_transport as tt
            raise tt.TelegramTransportError("network error")
        ok = self.ok_chat_ids == "all" or chat_id in self.ok_chat_ids
        return {"ok": ok, "result": {}}


def test_send_report_confirms_delivery_per_recipient():
    transport = _FakeTransport(ok_chat_ids="all")
    results = reports.send_report_to_admins(transport, [111, 222], "report text")
    assert results[111]["ok"] is True
    assert results[222]["ok"] is True


def test_send_report_one_recipient_failure_does_not_block_others():
    transport = _FakeTransport(ok_chat_ids={222})  # 111 will get ok=false
    results = reports.send_report_to_admins(transport, [111, 222], "report text")
    assert results[111]["ok"] is False
    assert results[222]["ok"] is True
    assert len(transport.sent) == 2  # both were attempted


def test_send_report_transport_error_recorded_not_raised():
    transport = _FakeTransport(raise_for_chat_ids={111})
    results = reports.send_report_to_admins(transport, [111], "report text")
    assert results[111]["ok"] is False
    assert "network error" in results[111]["error"]


def test_send_report_long_text_sends_all_chunks_to_each_recipient():
    long_text = "\n".join(f"line {i}" for i in range(2000))
    transport = _FakeTransport(ok_chat_ids="all")
    reports.send_report_to_admins(transport, [111], long_text)
    sent_to_111 = [s for s in transport.sent if s[0] == 111]
    assert len(sent_to_111) > 1
    assert "".join(s[1] for s in sent_to_111) == "".join(reports.chunk_telegram_message(long_text))


# ---------- scheduling: idempotent once-per-day ------------------------------------------------

def test_should_send_report_true_after_scheduled_time_first_time_today():
    settings = tc.TelegramSettings(report_hour=9, report_minute=0, report_timezone="UTC")
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert reports.should_send_scheduled_report(settings, last_sent_date=None, now=now) is True


def test_should_send_report_false_before_scheduled_time():
    settings = tc.TelegramSettings(report_hour=9, report_minute=0, report_timezone="UTC")
    now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert reports.should_send_scheduled_report(settings, last_sent_date=None, now=now) is False


def test_should_send_report_false_if_already_sent_today():
    settings = tc.TelegramSettings(report_hour=9, report_minute=0, report_timezone="UTC")
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert reports.should_send_scheduled_report(settings, last_sent_date="2026-01-01", now=now) is False


def test_should_send_report_true_again_next_day():
    settings = tc.TelegramSettings(report_hour=9, report_minute=0, report_timezone="UTC")
    now = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)
    assert reports.should_send_scheduled_report(settings, last_sent_date="2026-01-01", now=now) is True


def test_should_send_report_respects_configured_timezone_not_utc():
    """09:00 Asia/Tehran (UTC+03:30) is 05:30 UTC -- a UTC-naive check would wrongly say
    it's not yet time at 06:00 UTC, when it's actually already past 09:00 local."""
    settings = tc.TelegramSettings(report_hour=9, report_minute=0, report_timezone="Asia/Tehran")
    now = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)  # 09:30 Tehran
    assert reports.should_send_scheduled_report(settings, last_sent_date=None, now=now) is True


def test_should_send_report_never_backlogs_missed_days():
    """A report missed for 3 days (process was down) does not queue 3 reports -- the next
    check after resuming sends exactly one, for the current day."""
    settings = tc.TelegramSettings(report_hour=0, report_minute=0, report_timezone="UTC")
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    assert reports.should_send_scheduled_report(settings, last_sent_date="2026-01-01", now=now) is True
    # after sending, marking today as sent prevents a second send today regardless of the gap
    assert reports.should_send_scheduled_report(settings, last_sent_date="2026-01-05", now=now) is False


# ---------- offset/date persistence --------------------------------------------------------------

def test_load_last_report_sent_date_defaults_to_none(tmp_path):
    assert reports.load_last_report_sent_date(str(tmp_path)) is None


def test_save_and_load_last_report_sent_date_roundtrips(tmp_path):
    reports.save_last_report_sent_date(str(tmp_path), "2026-03-10")
    assert reports.load_last_report_sent_date(str(tmp_path)) == "2026-03-10"


def test_load_last_report_sent_date_malformed_file_falls_back_to_none(tmp_path):
    path = os.path.join(str(tmp_path), "telegram-report-schedule.json")
    with open(path, "w") as f:
        f.write("{not valid json")
    assert reports.load_last_report_sent_date(str(tmp_path)) is None
