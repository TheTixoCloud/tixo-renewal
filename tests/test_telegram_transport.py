"""Tests for telegram_transport.py (v1.8.0 Part 3): Direct mode (mocked HTTP, same DI
pattern as writer.py), SOCKS5 mode (a REAL from-scratch handshake tested against a real
local SOCKS5-speaking TCP test server -- not mocked at the protocol level), and relay
request signing/verification (replay/tamper/expiry rejection).
"""
import os
import socket
import struct
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import telegram_transport as tt


# ---------- Direct transport ------------------------------------------------------------------

def test_direct_transport_calls_telegram_api_url_shape():
    captured = {}
    def fake_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        return '{"ok": true, "result": {}}'
    transport = tt.DirectTransport(bot_token="123:ABC")
    result = transport.call("getMe", http_post=fake_post)
    assert result == {"ok": True, "result": {}}
    assert captured["url"] == "https://api.telegram.org/bot123:ABC/getMe"


def test_direct_transport_sends_params_as_json_body():
    captured = {}
    def fake_post(url, data, timeout):
        captured["data"] = data
        return '{"ok": true}'
    transport = tt.DirectTransport(bot_token="t")
    transport.call("sendMessage", {"chat_id": 42, "text": "hi"}, http_post=fake_post)
    import json
    assert json.loads(captured["data"]) == {"chat_id": 42, "text": "hi"}


def test_direct_transport_redacts_token_on_network_failure():
    def failing_post(url, data, timeout):
        raise OSError(f"connection failed to {url}")
    transport = tt.DirectTransport(bot_token="SECRETTOKEN123")
    with pytest.raises(tt.TelegramTransportError) as exc_info:
        transport.call("getMe", http_post=failing_post)
    assert "SECRETTOKEN123" not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)


def test_direct_transport_raises_on_invalid_json_response():
    transport = tt.DirectTransport(bot_token="t")
    with pytest.raises(tt.TelegramTransportError):
        transport.call("getMe", http_post=lambda u, d, t: "not json")


# ---------- DirectTransport.send_document (backup delivery) ----------------------------------

def test_send_document_uploads_multipart_with_file_bytes(tmp_path):
    file_path = str(tmp_path / "archive.tar.gz")
    with open(file_path, "wb") as f:
        f.write(b"fake archive bytes")

    captured = {}
    def fake_post(url, data, timeout, content_type):
        captured["url"] = url
        captured["data"] = data
        captured["content_type"] = content_type
        return '{"ok": true, "result": {"message_id": 1}}'

    transport = tt.DirectTransport(bot_token="123:ABC")
    result = transport.send_document(chat_id=42, file_path=file_path, caption="backup",
                                        http_post=fake_post)
    assert result == {"ok": True, "result": {"message_id": 1}}
    assert captured["url"] == "https://api.telegram.org/bot123:ABC/sendDocument"
    assert "multipart/form-data" in captured["content_type"]
    assert b"fake archive bytes" in captured["data"]
    assert b'name="chat_id"' in captured["data"]
    assert b'name="caption"' in captured["data"]


def test_send_document_refuses_oversized_file(tmp_path):
    file_path = str(tmp_path / "big.tar.gz")
    with open(file_path, "wb") as f:
        f.write(b"x" * 1000)
    transport = tt.DirectTransport(bot_token="t")
    with pytest.raises(tt.TelegramTransportError, match="exceeds max_size_bytes"):
        transport.send_document(chat_id=1, file_path=file_path, max_size_bytes=500)


def test_send_document_redacts_token_on_network_failure(tmp_path):
    file_path = str(tmp_path / "f.tar.gz")
    with open(file_path, "wb") as f:
        f.write(b"data")
    def failing_post(url, data, timeout, content_type):
        raise OSError(f"connection failed to {url}")
    transport = tt.DirectTransport(bot_token="SECRETTOKEN123")
    with pytest.raises(tt.TelegramTransportError) as exc_info:
        transport.send_document(chat_id=1, file_path=file_path, http_post=failing_post)
    assert "SECRETTOKEN123" not in str(exc_info.value)


def test_socks5_transport_send_document_not_implemented():
    proxy = tt.Socks5ProxyConfig(host="x", port=1080)
    transport = tt.Socks5Transport(bot_token="t", proxy=proxy)
    with pytest.raises(NotImplementedError, match="SOCKS5"):
        transport.send_document(chat_id=1, file_path="/tmp/whatever")


def test_relay_client_transport_send_document_not_implemented():
    transport = tt.RelayClientTransport(bot_token="t", shared_secret="s",
                                          relay_host="relay.example", relay_port=8765)
    with pytest.raises(NotImplementedError, match="RELAY"):
        transport.send_document(chat_id=1, file_path="/tmp/whatever")


# ---------- SOCKS5 transport: real handshake against a real local test server ----------------

class _FakeSocks5Server(threading.Thread):
    """A minimal, REAL SOCKS5 server (RFC 1928) speaking actual TCP/bytes -- not a mock of
    socks5_connect's behavior. Accepts NO-AUTH or configured username/password, then accepts
    a CONNECT request and, instead of actually reaching the target, immediately writes a
    canned HTTP response back down the tunnel (since real internet access to
    api.telegram.org isn't available/appropriate in a test). This exercises the REAL
    handshake bytes-on-the-wire logic in telegram_transport.socks5_connect, which is the
    part most likely to have a subtle protocol bug."""

    def __init__(self, require_auth: bool = False, expected_user="u", expected_pass="p"):
        super().__init__(daemon=True)
        self.require_auth = require_auth
        self.expected_user = expected_user
        self.expected_pass = expected_pass
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(1)
        self.port = self.server_sock.getsockname()[1]
        self.last_target = None

    def run(self):
        conn, _ = self.server_sock.accept()
        try:
            self._handle(conn)
        except (OSError, IndexError, struct.error):
            # The client-side test may deliberately disconnect early (e.g. refusing to
            # send credentials it doesn't have) -- that's an expected end of this fake
            # server's conversation, not a real protocol bug to surface as thread noise.
            pass
        finally:
            conn.close()
            self.server_sock.close()

    def _handle(self, conn):
        greeting = conn.recv(2)  # VER, NMETHODS
        nmethods = greeting[1]
        methods = conn.recv(nmethods) if nmethods else b""
        if self.require_auth:
            conn.sendall(bytes([0x05, 0x02]))
            auth = conn.recv(256)
            ulen = auth[1]
            user = auth[2:2 + ulen].decode()
            plen = auth[2 + ulen]
            pw = auth[3 + ulen:3 + ulen + plen].decode()
            ok = user == self.expected_user and pw == self.expected_pass
            conn.sendall(bytes([0x01, 0x00 if ok else 0x01]))
            if not ok:
                return
        else:
            conn.sendall(bytes([0x05, 0x00]))

        request = conn.recv(4)
        atyp = request[3]
        if atyp == 0x03:
            length = conn.recv(1)[0]
            host = conn.recv(length).decode()
        elif atyp == 0x01:
            host = socket.inet_ntoa(conn.recv(4))
        port = struct.unpack(">H", conn.recv(2))[0]
        self.last_target = (host, port)

        conn.sendall(bytes([0x05, 0x00, 0x00, 0x01]) + socket.inet_aton("127.0.0.1") +
                     struct.pack(">H", 0))
        # Write a canned plaintext response so the test can verify data flows through
        # the tunnel -- the real Socks5Transport wraps this in TLS, so this low-level
        # test only exercises socks5_connect() directly, not the TLS+HTTP layer.
        conn.sendall(b"TUNNEL-OK")


def test_socks5_connect_real_handshake_no_auth():
    server = _FakeSocks5Server(require_auth=False)
    server.start()
    sock = tt.socks5_connect("127.0.0.1", server.port, "api.telegram.org", 443, timeout=5)
    try:
        data = sock.recv(16)
        assert data == b"TUNNEL-OK"
    finally:
        sock.close()
    server.join(timeout=2)
    assert server.last_target == ("api.telegram.org", 443)


def test_socks5_connect_real_handshake_with_auth_success():
    server = _FakeSocks5Server(require_auth=True, expected_user="admin", expected_pass="secret")
    server.start()
    sock = tt.socks5_connect("127.0.0.1", server.port, "api.telegram.org", 443,
                              username="admin", password="secret", timeout=5)
    try:
        assert sock.recv(16) == b"TUNNEL-OK"
    finally:
        sock.close()


def test_socks5_connect_auth_failure_raises():
    server = _FakeSocks5Server(require_auth=True, expected_user="admin", expected_pass="secret")
    server.start()
    with pytest.raises(tt.Socks5Error):
        tt.socks5_connect("127.0.0.1", server.port, "api.telegram.org", 443,
                           username="admin", password="WRONG", timeout=5)


def test_socks5_connect_requires_auth_but_none_configured():
    server = _FakeSocks5Server(require_auth=True)
    server.start()
    with pytest.raises(tt.Socks5Error, match="requires username/password"):
        tt.socks5_connect("127.0.0.1", server.port, "api.telegram.org", 443, timeout=5)


def test_socks5_connect_ip_literal_target_uses_ipv4_address_type():
    server = _FakeSocks5Server(require_auth=False)
    server.start()
    sock = tt.socks5_connect("127.0.0.1", server.port, "203.0.113.5", 443, timeout=5)
    sock.recv(16)
    sock.close()
    server.join(timeout=2)
    assert server.last_target == ("203.0.113.5", 443)


def test_socks5_transport_test_connectivity_reports_failure_cleanly():
    """No real proxy listening at all -- test_connectivity() must report ok=False, never
    raise, so a settings screen can show a clean error instead of crashing."""
    proxy = tt.Socks5ProxyConfig(host="127.0.0.1", port=1, timeout_seconds=1)  # port 1: nothing listens
    transport = tt.Socks5Transport(bot_token="t", proxy=proxy)
    result = transport.test_connectivity()
    assert result["ok"] is False


# ---------- relay request signing/verification -------------------------------------------------

def test_sign_and_verify_relay_request_roundtrips():
    envelope = tt.sign_relay_request("shared-secret", {"method": "sendMessage", "chat_id": 1})
    result = tt.verify_relay_request("shared-secret", envelope, seen_nonces=set())
    assert result["valid"] is True
    assert result["payload"] == {"method": "sendMessage", "chat_id": 1}


def test_verify_relay_request_rejects_wrong_secret():
    envelope = tt.sign_relay_request("shared-secret", {"x": 1})
    result = tt.verify_relay_request("wrong-secret", envelope, seen_nonces=set())
    assert result["valid"] is False
    assert "signature" in result["reason"]


def test_verify_relay_request_rejects_tampered_payload():
    envelope = tt.sign_relay_request("shared-secret", {"amount": 1})
    envelope["payload"]["amount"] = 9999  # tamper after signing
    result = tt.verify_relay_request("shared-secret", envelope, seen_nonces=set())
    assert result["valid"] is False


def test_verify_relay_request_rejects_replayed_nonce():
    envelope = tt.sign_relay_request("shared-secret", {"x": 1})
    seen = set()
    first = tt.verify_relay_request("shared-secret", envelope, seen_nonces=seen)
    assert first["valid"] is True
    seen.add(envelope["nonce"])  # caller's job to persist seen nonces
    second = tt.verify_relay_request("shared-secret", envelope, seen_nonces=seen)
    assert second["valid"] is False
    assert "replay" in second["reason"]


def test_verify_relay_request_rejects_expired_timestamp():
    old_time = datetime.now(timezone.utc) - timedelta(hours=1)
    envelope = tt.sign_relay_request("shared-secret", {"x": 1}, timestamp=old_time.isoformat())
    result = tt.verify_relay_request("shared-secret", envelope, seen_nonces=set(), max_age_seconds=300)
    assert result["valid"] is False
    assert "window" in result["reason"]


def test_verify_relay_request_missing_fields_rejected():
    result = tt.verify_relay_request("secret", {"payload": {}}, seen_nonces=set())
    assert result["valid"] is False
    assert "missing" in result["reason"]


def test_verify_relay_request_unparseable_timestamp_rejected():
    envelope = tt.sign_relay_request("secret", {"x": 1})
    envelope["timestamp"] = "not-a-timestamp"
    # signature now won't match since timestamp changed after signing -- re-sign with the bad ts
    import hmac, hashlib, json as _json
    canonical = _json.dumps({"payload": envelope["payload"], "nonce": envelope["nonce"],
                              "timestamp": envelope["timestamp"]}, sort_keys=True, separators=(",", ":"))
    envelope["signature"] = hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest()
    result = tt.verify_relay_request("secret", envelope, seen_nonces=set())
    assert result["valid"] is False
    assert "timestamp" in result["reason"]
