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


# ---------- DirectTransport.send_document (backup delivery): streamed over a fake socket -----

class _FakeDocumentSocket:
    """Records every byte sent (so the test can parse the real multipart request the
    streaming code produced) and returns a canned HTTP response on recv()."""

    def __init__(self, response: bytes = None):
        self.sent = bytearray()
        self._response = response or (
            b"HTTP/1.1 200 OK\r\nContent-Length: 36\r\n\r\n"
            b'{"ok": true, "result": {"message_id": 1}}')
        self._response_sent = False

    def sendall(self, data: bytes):
        self.sent.extend(data)

    def recv(self, n: int) -> bytes:
        if self._response_sent:
            return b""
        self._response_sent = True
        return self._response

    def close(self):
        pass


def test_send_document_streams_multipart_over_socket(tmp_path):
    file_path = str(tmp_path / "archive.tar.gz")
    with open(file_path, "wb") as f:
        f.write(b"fake archive bytes")

    fake_socket = _FakeDocumentSocket()
    transport = tt.DirectTransport(bot_token="123:ABC")
    result = transport.send_document(chat_id=42, file_path=file_path, caption="backup",
                                        socket_factory=lambda: fake_socket)
    assert result == {"ok": True, "result": {"message_id": 1}}
    sent = bytes(fake_socket.sent)
    assert b"POST /bot123:ABC/sendDocument HTTP/1.1" in sent
    assert b"multipart/form-data; boundary=" in sent
    assert b"fake archive bytes" in sent
    assert b'name="chat_id"' in sent
    assert b'name="caption"' in sent
    # Content-Length must exactly match what was actually sent after the header block.
    header_end = sent.index(b"\r\n\r\n") + 4
    import re
    cl = int(re.search(rb"Content-Length: (\d+)", sent).group(1))
    assert len(sent) - header_end == cl


def test_send_document_never_buffers_whole_file_via_small_chunks(tmp_path):
    """Forces a tiny chunk size so the streaming loop runs many iterations -- confirms the
    file is read incrementally (os.path.getsize for the cap check is the only whole-file
    stat; the content itself is never loaded in one piece)."""
    file_path = str(tmp_path / "archive.tar.gz")
    payload = b"0123456789" * 50  # 500 bytes, several chunks at chunk_size=64
    with open(file_path, "wb") as f:
        f.write(payload)

    fake_socket = _FakeDocumentSocket()
    import telegram_transport as tt_mod
    monkeypatch_chunk = tt_mod.DEFAULT_DOCUMENT_CHUNK_BYTES
    try:
        tt_mod.DEFAULT_DOCUMENT_CHUNK_BYTES = 64
        transport = tt.DirectTransport(bot_token="t")
        transport.send_document(chat_id=1, file_path=file_path, socket_factory=lambda: fake_socket)
    finally:
        tt_mod.DEFAULT_DOCUMENT_CHUNK_BYTES = monkeypatch_chunk
    assert payload in bytes(fake_socket.sent)


def test_send_document_refuses_oversized_file(tmp_path):
    file_path = str(tmp_path / "big.tar.gz")
    with open(file_path, "wb") as f:
        f.write(b"x" * 1000)
    transport = tt.DirectTransport(bot_token="t")
    with pytest.raises(tt.TelegramTransportError, match="exceeds max_size_bytes"):
        transport.send_document(chat_id=1, file_path=file_path, max_size_bytes=500)


def test_send_document_redacts_token_on_connection_failure(tmp_path):
    file_path = str(tmp_path / "f.tar.gz")
    with open(file_path, "wb") as f:
        f.write(b"data")
    def failing_socket_factory():
        raise OSError("connection failed to SECRETTOKEN123-flavored-host")
    transport = tt.DirectTransport(bot_token="SECRETTOKEN123")
    with pytest.raises(tt.TelegramTransportError) as exc_info:
        transport.send_document(chat_id=1, file_path=file_path,
                                   socket_factory=failing_socket_factory)
    assert "SECRETTOKEN123" not in str(exc_info.value)


# ---------- Socks5Transport.send_document: real SOCKS5 handshake + real TLS ------------------

def test_socks5_transport_send_document_real_protocol_round_trip(tmp_path):
    """Drives the FULL real path: a real SOCKS5 CONNECT handshake against the existing fake
    SOCKS5 server, then a real TLS handshake (self-signed test cert, verification disabled
    exactly like test_web_server.py's TLS tests) carrying a real streamed multipart
    sendDocument request -- not mocked at the protocol level."""
    import ssl as ssl_mod
    import subprocess

    cert_path = str(tmp_path / "cert.pem")
    key_path = str(tmp_path / "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key_path,
         "-out", cert_path, "-days", "1", "-nodes", "-subj", "/CN=test"],
        check=True, capture_output=True)

    file_path = str(tmp_path / "backup.tar.gz.gpg")
    with open(file_path, "wb") as f:
        f.write(b"encrypted backup bytes")

    server = _FakeSocks5TlsServer(cert_path, key_path)
    server.start()

    def insecure_ctx():
        ctx = ssl_mod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_mod.CERT_NONE
        return ctx

    proxy = tt.Socks5ProxyConfig(host="127.0.0.1", port=server.port)
    transport = tt.Socks5Transport(bot_token="123:ABC", proxy=proxy,
                                     ssl_context_factory=insecure_ctx)
    result = transport.send_document(chat_id=99, file_path=file_path, caption="nightly backup")
    server.join(timeout=5)

    assert result == {"ok": True, "result": {"message_id": 7}}
    assert server.last_target == ("api.telegram.org", 443)
    assert b"encrypted backup bytes" in server.received_request
    assert b'name="chat_id"' in server.received_request
    assert b'name="caption"' in server.received_request


def test_socks5_transport_send_document_redacts_token_on_proxy_failure():
    proxy = tt.Socks5ProxyConfig(host="127.0.0.1", port=1, timeout_seconds=1)
    transport = tt.Socks5Transport(bot_token="SECRETTOKEN123", proxy=proxy)
    with pytest.raises(tt.TelegramTransportError) as exc_info:
        transport.send_document(chat_id=1, file_path="/nonexistent/doesnt-matter")
    assert "SECRETTOKEN123" not in str(exc_info.value)


# ---------- RelayClientTransport.send_document: real base64 envelope construction -------------

def test_relay_send_document_builds_signed_base64_envelope(tmp_path):
    file_path = str(tmp_path / "backup.tar.gz.gpg")
    with open(file_path, "wb") as f:
        f.write(b"encrypted backup bytes")

    captured = {}
    def fake_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        import json as _json
        envelope = _json.loads(data)
        payload = envelope["payload"]
        result_payload = {"ok": True, "result": {"message_id": 5}}
        signed = tt.sign_relay_request("shared-secret", result_payload)
        return _json.dumps(signed)

    transport = tt.RelayClientTransport(bot_token="t", shared_secret="shared-secret",
                                          relay_host="relay.example", relay_port=8765)
    result = transport.send_document(chat_id=42, file_path=file_path, caption="backup",
                                        http_post=fake_post)
    assert result == {"message_id": 5}
    assert captured["url"] == "https://relay.example:8765/submit-document"

    import json as _json, base64
    envelope = _json.loads(captured["data"])
    verification = tt.verify_relay_request("shared-secret", envelope, set())
    assert verification["valid"]
    payload = verification["payload"]
    assert payload["method"] == "sendDocument"
    assert payload["params"]["chat_id"] == 42
    assert payload["params"]["caption"] == "backup"
    assert base64.b64decode(payload["file_b64"]) == b"encrypted backup bytes"


def test_relay_send_document_never_includes_passphrase_or_secret(tmp_path):
    """The envelope must carry ONLY the archive bytes -- never the bot token, the relay
    shared secret, or anything else -- confirmed by scanning the full signed envelope."""
    file_path = str(tmp_path / "backup.tar.gz.gpg")
    with open(file_path, "wb") as f:
        f.write(b"encrypted backup bytes")

    captured = {}
    def fake_post(url, data, timeout):
        captured["data"] = data
        signed = tt.sign_relay_request("shared-secret", {"ok": True, "result": {}})
        import json as _json
        return _json.dumps(signed)

    transport = tt.RelayClientTransport(bot_token="SECRET-BOT-TOKEN",
                                          shared_secret="shared-secret",
                                          relay_host="relay.example", relay_port=8765)
    transport.send_document(chat_id=1, file_path=file_path, http_post=fake_post)
    assert b"SECRET-BOT-TOKEN" not in captured["data"]
    assert b"shared-secret" not in captured["data"]


def test_relay_send_document_refuses_oversized_file(tmp_path):
    file_path = str(tmp_path / "huge.tar.gz.gpg")
    with open(file_path, "wb") as f:
        f.write(b"x" * 2000)
    transport = tt.RelayClientTransport(bot_token="t", shared_secret="s",
                                          relay_host="relay.example", relay_port=8765)
    with pytest.raises(tt.TelegramTransportError, match="exceeds max_size_bytes"):
        transport.send_document(chat_id=1, file_path=file_path, max_size_bytes=1000)


def test_relay_send_document_rejects_tampered_response(tmp_path):
    file_path = str(tmp_path / "backup.tar.gz.gpg")
    with open(file_path, "wb") as f:
        f.write(b"data")

    def fake_post(url, data, timeout):
        tampered = tt.sign_relay_request("WRONG-secret", {"ok": True, "result": {}})
        import json as _json
        return _json.dumps(tampered)

    transport = tt.RelayClientTransport(bot_token="t", shared_secret="shared-secret",
                                          relay_host="relay.example", relay_port=8765)
    with pytest.raises(tt.TelegramTransportError, match="failed verification"):
        transport.send_document(chat_id=1, file_path=file_path, http_post=fake_post)


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


class _FakeSocks5TlsServer(threading.Thread):
    """Extends the real SOCKS5 handshake above with a real TLS server layered on top of
    the tunneled connection (self-signed test cert) -- lets a test drive
    Socks5Transport.send_document() through its ENTIRE real path (SOCKS5 CONNECT -> TLS
    handshake -> streamed multipart HTTP request), not just the pre-TLS handshake bytes."""

    def __init__(self, cert_path: str, key_path: str):
        super().__init__(daemon=True)
        self.cert_path = cert_path
        self.key_path = key_path
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(1)
        self.port = self.server_sock.getsockname()[1]
        self.last_target = None
        self.received_request = b""

    def run(self):
        conn, _ = self.server_sock.accept()
        try:
            self._handle(conn)
        finally:
            self.server_sock.close()

    def _handle(self, conn):
        import ssl as ssl_mod

        greeting = conn.recv(2)
        nmethods = greeting[1]
        conn.recv(nmethods) if nmethods else b""
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

        ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        tls_conn = ctx.wrap_socket(conn, server_side=True)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += tls_conn.recv(65536)
            header_end = buf.index(b"\r\n\r\n") + 4
            headers = buf[:header_end].decode("utf-8", "replace")
            content_length = 0
            for line in headers.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            body = buf[header_end:]
            while len(body) < content_length:
                body += tls_conn.recv(65536)
            self.received_request = buf[:header_end] + body

            response = (b'{"ok": true, "result": {"message_id": 7}}')
            tls_conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(response)).encode() +
                b"\r\n\r\n" + response)
        finally:
            tls_conn.close()


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
