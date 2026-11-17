"""Tests for relay_worker.py (v1.8.0 Part 13): the separate process meant to run on a VPS
that CAN reach Telegram, for an Iran-side host that cannot. Most tests call
RelayWorkerApp.handle_request() directly (pure, socket-free core, with a fake injected
Telegram call); one spins up a REAL local server and drives it through
telegram_transport.RelayClientTransport.call() with a monkeypatched http_post, simulating
the full two-host protocol on localhost per the mission's explicit allowance to simulate a
second host locally when a real one is unavailable.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import relay_worker as rw
import telegram_transport as tt

SHARED_SECRET = "test-shared-secret"
BOT_TOKEN = "123:ABC"


def _app(tmp_path, telegram_call=None, max_age=300):
    return rw.RelayWorkerApp(BOT_TOKEN, SHARED_SECRET, str(tmp_path / "nonces.sqlite3"),
                              max_envelope_age_seconds=max_age, telegram_call=telegram_call)


def test_valid_envelope_is_executed_and_signed_result_returned(tmp_path):
    calls = []

    def fake_call(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 42}}

    app = _app(tmp_path, telegram_call=fake_call)
    envelope = tt.sign_relay_request(SHARED_SECRET,
                                      {"method": "sendMessage", "params": {"chat_id": 1, "text": "hi"}})
    status, body = app.handle_request("POST", "/submit", json.dumps(envelope).encode())
    assert status == 200
    assert calls == [("sendMessage", {"chat_id": 1, "text": "hi"})]

    signed_result = json.loads(body)
    verification = tt.verify_relay_request(SHARED_SECRET, signed_result, set())
    assert verification["valid"]
    assert verification["payload"]["ok"] is True
    assert verification["payload"]["result"] == {"ok": True, "result": {"message_id": 42}}


def test_bad_signature_rejected(tmp_path):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request("WRONG-secret", {"method": "getMe", "params": {}})
    status, body = app.handle_request("POST", "/submit", json.dumps(envelope).encode())
    assert status == 403
    assert "signature" in json.loads(body)["error"]


def test_replayed_nonce_rejected(tmp_path):
    def fake_call(method, params):
        return {"ok": True}
    app = _app(tmp_path, telegram_call=fake_call)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"method": "getMe", "params": {}})
    body = json.dumps(envelope).encode()

    status1, _ = app.handle_request("POST", "/submit", body)
    assert status1 == 200
    status2, resp2 = app.handle_request("POST", "/submit", body)
    assert status2 == 403
    assert "replay" in json.loads(resp2)["error"]


def test_stale_timestamp_rejected(tmp_path):
    import datetime
    app = _app(tmp_path, max_age=5)
    old_ts = (datetime.datetime.now(datetime.timezone.utc) -
              datetime.timedelta(seconds=60)).isoformat()
    envelope = tt.sign_relay_request(SHARED_SECRET, {"method": "getMe", "params": {}},
                                       timestamp=old_ts)
    status, body = app.handle_request("POST", "/submit", json.dumps(envelope).encode())
    assert status == 403
    assert "window" in json.loads(body)["error"]


def test_malformed_body_returns_400(tmp_path):
    app = _app(tmp_path)
    status, body = app.handle_request("POST", "/submit", b"not json at all")
    assert status == 400


def test_unknown_route_returns_404(tmp_path):
    app = _app(tmp_path)
    status, _ = app.handle_request("GET", "/nope", b"")
    assert status == 404


def test_payload_missing_method_returns_400(tmp_path):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"params": {}})
    status, body = app.handle_request("POST", "/submit", json.dumps(envelope).encode())
    assert status == 400


def test_telegram_transport_error_surfaces_as_signed_failure(tmp_path):
    def failing_call(method, params):
        raise tt.TelegramTransportError("simulated network failure")

    app = _app(tmp_path, telegram_call=failing_call)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"method": "getMe", "params": {}})
    status, body = app.handle_request("POST", "/submit", json.dumps(envelope).encode())
    assert status == 200  # the worker itself succeeded; the Telegram call is what failed
    signed_result = json.loads(body)
    verification = tt.verify_relay_request(SHARED_SECRET, signed_result, set())
    assert verification["valid"]
    assert verification["payload"]["ok"] is False
    assert "simulated network failure" in verification["payload"]["error"]


# ---------- RelayClientTransport (Iran-side) ---------------------------------------------------

def test_relay_client_transport_success_via_injected_http_post(tmp_path):
    def fake_call(method, params):
        return {"ok": True, "result": {"id": 7}}
    app = _app(tmp_path, telegram_call=fake_call)

    def fake_http_post(url, data, timeout):
        envelope = json.loads(data.decode())
        status, body = app.handle_request("POST", "/submit", json.dumps(envelope).encode())
        assert status == 200
        return body.decode()

    client = tt.RelayClientTransport(bot_token=BOT_TOKEN, shared_secret=SHARED_SECRET,
                                       relay_host="relay.example", relay_port=8765)
    result = client.call("sendMessage", {"chat_id": 1}, http_post=fake_http_post)
    assert result == {"ok": True, "result": {"id": 7}}


def test_relay_client_transport_rejects_tampered_response(tmp_path):
    def fake_http_post(url, data, timeout):
        tampered = tt.sign_relay_request("WRONG-secret", {"ok": True, "result": {}})
        return json.dumps(tampered)

    client = tt.RelayClientTransport(bot_token=BOT_TOKEN, shared_secret=SHARED_SECRET,
                                       relay_host="relay.example", relay_port=8765)
    with pytest.raises(tt.TelegramTransportError, match="failed verification"):
        client.call("getMe", http_post=fake_http_post)


def test_relay_client_transport_raises_on_telegram_failure_reported_by_worker(tmp_path):
    def fake_http_post(url, data, timeout):
        signed = tt.sign_relay_request(SHARED_SECRET, {"ok": False, "error": "boom"})
        return json.dumps(signed)

    client = tt.RelayClientTransport(bot_token=BOT_TOKEN, shared_secret=SHARED_SECRET,
                                       relay_host="relay.example", relay_port=8765)
    with pytest.raises(tt.TelegramTransportError, match="boom"):
        client.call("getMe", http_post=fake_http_post)


# ---------- real local socket end-to-end (simulated second host) -------------------------------

def test_real_server_end_to_end_simulated_second_host(tmp_path, monkeypatch):
    """Spins up a REAL relay_worker server on a real localhost port and drives it through
    RelayClientTransport over a genuine socket -- the mission explicitly allows simulating
    the second host locally when a real remote one is unavailable; this is that simulation,
    clearly NOT a real cross-host validation."""
    import urllib.request

    def fake_telegram_call(method, params):
        return {"ok": True, "result": {"method_echoed": method}}

    nonce_db = str(tmp_path / "nonces.sqlite3")
    server, thread = rw.run_server_in_thread(BOT_TOKEN, SHARED_SECRET, nonce_db,
                                               "127.0.0.1", 0)
    try:
        actual_port = server.server_address[1]

        # The running worker builds its own DirectTransport internally (telegram_call=None in
        # production); monkeypatch that shared class so the real HTTP round trip exercises
        # genuine sockets end to end while the simulated "Telegram" call itself stays local.
        import telegram_transport as tt_mod
        monkeypatch.setattr(tt_mod.DirectTransport, "call",
                             lambda self, method, params=None, http_post=None: fake_telegram_call(method, params))

        client = tt.RelayClientTransport(bot_token=BOT_TOKEN, shared_secret=SHARED_SECRET,
                                           relay_host="127.0.0.1", relay_port=actual_port,
                                           use_tls=False)
        result = client.call("sendMessage", {"chat_id": 99, "text": "ping"})
        assert result == {"ok": True, "result": {"method_echoed": "sendMessage"}}
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------- renewal_cli `relay-worker-run` command ----------------------------------------------

def test_cmd_relay_worker_run_blocked_when_disabled(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_relay_worker_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_relay_worker_run_blocked_when_secrets_missing(tmp_path, capsys):
    import renewal_cli
    import telegram_config as tgc
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    tgc.save_relay_worker_settings(renewal_cli.CONFIG_DIR,
                                     tgc.RelayWorkerSettings(enabled=True, bind_port=18766))

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_relay_worker_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_relay_worker_run_starts_and_stops_with_max_iterations(tmp_path, capsys):
    import renewal_cli
    import telegram_config as tgc
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    tgc.save_relay_worker_settings(renewal_cli.CONFIG_DIR,
                                     tgc.RelayWorkerSettings(enabled=True, bind_host="127.0.0.1",
                                                              bind_port=18767))
    tgc.store_relay_worker_secrets(
        tgc.default_relay_worker_secrets_path(renewal_cli.CONFIG_DIR),
        bot_token="123:ABC", shared_secret="shh")

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_relay_worker_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Starting relay worker" in out
    assert "stopped" in out.lower()
