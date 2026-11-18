"""Tests for agent_config.py and agent_server.py (v1.8.0 Part 14): the lightweight,
read-only, authenticated remote management agent. Most tests call AgentApp.handle_request()
directly (pure, socket-free core); one spins up a real local server and drives it through
query_agent() over a genuine socket.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import agent_config as ac
import agent_server as ags
import telegram_transport as tt

SHARED_SECRET = "agent-shared-secret"


def _dirs(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    return config_dir, state_dir


def _app(tmp_path, max_age=300):
    config_dir, state_dir = _dirs(tmp_path)
    return ags.AgentApp(config_dir, state_dir, SHARED_SECRET, str(tmp_path / "nonces.sqlite3"),
                         max_envelope_age_seconds=max_age)


# ---------- agent_config.py --------------------------------------------------------------------

def test_agent_settings_defaults_disabled_and_localhost():
    settings = ac.AgentSettings()
    assert settings.enabled is False
    assert settings.bind_host == "127.0.0.1"


def test_agent_settings_rejects_invalid_port():
    with pytest.raises(ValueError):
        ac.AgentSettings(bind_port=99999)


def test_agent_settings_roundtrip(tmp_path):
    settings = ac.AgentSettings(enabled=True, bind_host="0.0.0.0", bind_port=9100)
    ac.save_agent_settings(str(tmp_path), settings)
    loaded = ac.load_agent_settings(str(tmp_path))
    assert loaded.enabled is True
    assert loaded.bind_host == "0.0.0.0"
    assert loaded.bind_port == 9100


def test_agent_settings_missing_file_returns_safe_default(tmp_path):
    loaded = ac.load_agent_settings(str(tmp_path))
    assert loaded.enabled is False


def test_agent_secrets_roundtrip_and_permissions(tmp_path):
    import stat
    path = str(tmp_path / "secrets.json")
    ac.store_agent_secrets(path, shared_secret="shh")
    loaded = ac.load_agent_secrets(path)
    assert loaded.shared_secret == "shh"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_agent_secrets_refuse_loose_permissions(tmp_path):
    path = str(tmp_path / "secrets.json")
    ac.store_agent_secrets(path, shared_secret="shh")
    os.chmod(path, 0o644)
    with pytest.raises(ac.AgentConfigSecurityError):
        ac.load_agent_secrets(path)


def test_agent_secrets_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        ac.load_agent_secrets("/nonexistent/secrets.json")


def test_rotate_agent_secret_invalidates_old_secret(tmp_path):
    path = str(tmp_path / "secrets.json")
    ac.store_agent_secrets(path, shared_secret="old")
    ac.rotate_agent_secret(path, "new")
    loaded = ac.load_agent_secrets(path)
    assert loaded.shared_secret == "new"


# ---------- AgentApp.handle_request (pure core) ------------------------------------------------

def test_valid_snapshot_query_succeeds(tmp_path):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"query": "snapshot"})
    status, body = app.handle_request("POST", "/query", json.dumps(envelope).encode())
    assert status == 200
    signed_result = json.loads(body)
    verification = tt.verify_relay_request(SHARED_SECRET, signed_result, set())
    assert verification["valid"]
    assert verification["payload"]["ok"] is True
    assert "host" in verification["payload"]["result"]


@pytest.mark.parametrize("query", list(ags.ALLOWED_QUERIES))
def test_each_allowed_query_succeeds(tmp_path, query):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"query": query})
    status, body = app.handle_request("POST", "/query", json.dumps(envelope).encode())
    assert status == 200
    signed_result = json.loads(body)
    verification = tt.verify_relay_request(SHARED_SECRET, signed_result, set())
    assert verification["valid"]
    assert verification["payload"]["ok"] is True


def test_disallowed_query_rejected_but_envelope_still_valid(tmp_path):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"query": "renew_now"})
    status, body = app.handle_request("POST", "/query", json.dumps(envelope).encode())
    assert status == 200
    signed_result = json.loads(body)
    verification = tt.verify_relay_request(SHARED_SECRET, signed_result, set())
    assert verification["valid"]
    assert verification["payload"]["ok"] is False
    assert "not allowed" in verification["payload"]["error"]


def test_bad_signature_rejected(tmp_path):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request("WRONG-secret", {"query": "snapshot"})
    status, body = app.handle_request("POST", "/query", json.dumps(envelope).encode())
    assert status == 403


def test_replayed_nonce_rejected(tmp_path):
    app = _app(tmp_path)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"query": "host"})
    body = json.dumps(envelope).encode()
    status1, _ = app.handle_request("POST", "/query", body)
    assert status1 == 200
    status2, resp2 = app.handle_request("POST", "/query", body)
    assert status2 == 403
    assert "replay" in json.loads(resp2)["error"]


def test_stale_timestamp_rejected(tmp_path):
    import datetime
    app = _app(tmp_path, max_age=5)
    old_ts = (datetime.datetime.now(datetime.timezone.utc) -
              datetime.timedelta(seconds=60)).isoformat()
    envelope = tt.sign_relay_request(SHARED_SECRET, {"query": "snapshot"}, timestamp=old_ts)
    status, _ = app.handle_request("POST", "/query", json.dumps(envelope).encode())
    assert status == 403


def test_unknown_route_returns_404(tmp_path):
    app = _app(tmp_path)
    status, _ = app.handle_request("GET", "/nope", b"")
    assert status == 404


def test_malformed_body_returns_400(tmp_path):
    app = _app(tmp_path)
    status, _ = app.handle_request("POST", "/query", b"not json")
    assert status == 400


def test_agent_never_calls_virtualizor_api(tmp_path, monkeypatch):
    """The whole point of the agent is to expose THIS host's already-computed status --
    never to make a fresh, potentially-abusable, Virtualizor API call on every remote
    query."""
    import writer
    def _boom(*a, **k):
        raise AssertionError("agent must never call the Virtualizor API directly")
    monkeypatch.setattr(writer, "call_vs_api", _boom)

    app = _app(tmp_path)
    envelope = tt.sign_relay_request(SHARED_SECRET, {"query": "snapshot"})
    status, body = app.handle_request("POST", "/query", json.dumps(envelope).encode())
    assert status == 200


# ---------- query_agent() (management-server-side client) --------------------------------------

def test_query_agent_success_via_injected_http_post(tmp_path):
    app = _app(tmp_path)

    def fake_http_post(url, data, timeout):
        envelope = json.loads(data.decode())
        status, body = app.handle_request("POST", "/query", json.dumps(envelope).encode())
        assert status == 200
        return body.decode()

    result = ags.query_agent("agent.example", 8766, SHARED_SECRET, "host",
                               http_post=fake_http_post)
    assert "cpu_percent" in result or isinstance(result, dict)


def test_query_agent_rejects_unknown_query():
    with pytest.raises(ValueError, match="unknown query"):
        ags.query_agent("agent.example", 8766, SHARED_SECRET, "delete_everything")


def test_query_agent_rejects_tampered_response():
    def fake_http_post(url, data, timeout):
        tampered = tt.sign_relay_request("WRONG-secret", {"ok": True, "result": {}})
        return json.dumps(tampered)
    with pytest.raises(ValueError, match="failed verification"):
        ags.query_agent("agent.example", 8766, SHARED_SECRET, "host", http_post=fake_http_post)


def test_query_agent_raises_on_agent_reported_error():
    def fake_http_post(url, data, timeout):
        signed = tt.sign_relay_request(SHARED_SECRET, {"ok": False, "error": "boom"})
        return json.dumps(signed)
    with pytest.raises(ValueError, match="boom"):
        ags.query_agent("agent.example", 8766, SHARED_SECRET, "host", http_post=fake_http_post)


# ---------- real local socket end-to-end --------------------------------------------------------

def test_real_server_end_to_end_over_a_real_socket(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    nonce_db = str(tmp_path / "nonces.sqlite3")
    server, thread = ags.run_server_in_thread(config_dir, state_dir, SHARED_SECRET, nonce_db,
                                                "127.0.0.1", 0)
    try:
        actual_port = server.server_address[1]
        result = ags.query_agent("127.0.0.1", actual_port, SHARED_SECRET, "snapshot",
                                   use_tls=False)
        assert isinstance(result, dict)
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------- renewal_cli `agent-run` command ------------------------------------------------------

def test_cmd_agent_run_blocked_when_disabled(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_agent_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_agent_run_blocked_when_secrets_missing(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    ac.save_agent_settings(renewal_cli.CONFIG_DIR, ac.AgentSettings(enabled=True, bind_port=18768))

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_agent_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_agent_run_starts_and_stops_with_max_iterations(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    ac.save_agent_settings(renewal_cli.CONFIG_DIR,
                            ac.AgentSettings(enabled=True, bind_host="127.0.0.1", bind_port=18769))
    ac.store_agent_secrets(ac.default_secrets_path(renewal_cli.CONFIG_DIR), shared_secret="shh")

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_agent_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Starting remote agent" in out
    assert "stopped" in out.lower()


def test_menu_items_includes_remote_agent_config():
    import menu
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Configure Remote Agent (optional, read-only)" in labels


def test_real_server_rejects_oversized_request_body(tmp_path):
    import http.client

    config_dir, state_dir = _dirs(tmp_path)
    server, thread = ags.run_server_in_thread(config_dir, state_dir, SHARED_SECRET,
                                                str(tmp_path / "nonces.sqlite3"), "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        oversized_body = "x" * (ags.MAX_REQUEST_BODY_BYTES + 1)
        conn.putrequest("POST", "/query")
        conn.putheader("Content-Length", str(len(oversized_body)))
        conn.endheaders()
        conn.send(oversized_body[:1024].encode())
        resp = conn.getresponse()
        assert resp.status == 413
        conn.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
