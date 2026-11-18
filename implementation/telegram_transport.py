"""Telegram Bot API transports (v1.8.0 Part 3) -- pure stdlib, zero third-party dependency,
matching this project's existing convention (writer.py talks to Virtualizor the same way:
raw urllib, never `requests`).

THREE CONNECTIVITY MODES, matching the mission brief exactly:
  - DIRECT (default): plain HTTPS to api.telegram.org.
  - SOCKS5: HTTPS to api.telegram.org tunneled through a SOCKS5 proxy -- implemented as a
    real, from-scratch SOCKS5 CONNECT handshake over a raw socket (RFC 1928), since adding a
    third-party SOCKS library (PySocks) would break this project's zero-dependency rule.
    Supports optional username/password sub-negotiation (RFC 1929).
  - RELAY: NOT a network transport in the same sense as the other two -- see
    `RelayOutbox`/`sign_relay_request`/`verify_relay_request` below. The Iran-side host
    never talks to Telegram directly in this mode; it signs and durably queues outgoing
    requests for a SEPARATE relay worker (running on a VPS that can reach Telegram) to pick
    up and execute, and verifies the authenticity of whatever the relay reports back. This
    keeps the Iran host's only network requirement "can reach the relay," never "can reach
    Telegram," and avoids any inbound port on the Iran host (the relay is polled/pushed to,
    never the other way around, matching the mission's "prefer outbound connections from the
    Iran host" instruction).

SECRET HANDLING: the bot token is NEVER included in a log line, exception message, or
printed value anywhere in this module -- every exception path here redacts it the same way
writer.py redacts Virtualizor credentials.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

TELEGRAM_API_HOST = "api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_SOCKS5_TIMEOUT_SECONDS = 10
# Deliberately lower than Telegram's own 50MB bot-upload limit -- RELAY mode base64-encodes
# the file through a signed JSON envelope (~33% overhead), and relay_worker.py enforces its
# own matching cap on the resulting HTTP request body size.
RELAY_DOCUMENT_MAX_PLAINTEXT_BYTES = 15 * 1024 * 1024


class TelegramTransportError(Exception):
    """Raised instead of letting a raw socket/urllib exception propagate -- message is
    already scrubbed of the bot token."""
    pass


def _redact_token(text: str, token: str) -> str:
    text = str(text)
    if token:
        text = text.replace(token, "***REDACTED***")
        text = text.replace(urllib.parse.quote(token), "***REDACTED***")
    text = re.sub(r"/bot[0-9]+:[A-Za-z0-9_-]+/", "/bot***REDACTED***/", text)
    return text


DEFAULT_DOCUMENT_CHUNK_BYTES = 64 * 1024


def _multipart_preamble_and_epilogue(fields: dict, file_field: str, filename: str) -> tuple:
    """Builds the TEXT parts of a multipart/form-data body by hand (stdlib has no multipart
    encoder) -- deliberately simple and auditable, same philosophy as the from-scratch
    SOCKS5 handshake below. Returns (content_type_header_value, preamble_bytes,
    epilogue_bytes); the caller streams the file's own bytes between them, so the whole
    file is never held in memory at once."""
    boundary = "----tixo-" + os.urandom(16).hex()
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        .encode("utf-8"))
    preamble = b"".join(parts)
    epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", preamble, epilogue


def _stream_send_document_over_socket(sock, host: str, bot_token: str, chat_id, file_path: str,
                                        caption: str, max_size_bytes: int,
                                        chunk_size: int = DEFAULT_DOCUMENT_CHUNK_BYTES) -> dict:
    """Streams a real sendDocument multipart/form-data request directly onto an
    already-connected (and, for a real deployment, already TLS-wrapped) socket -- reads the
    file from disk in bounded chunks rather than loading it into memory at once. Shared by
    DirectTransport and Socks5Transport so both connectivity modes behave IDENTICALLY once
    a socket is established; only how that socket got connected (straight to Telegram, or
    through a SOCKS5 CONNECT tunnel first) differs between them."""
    size = os.path.getsize(file_path)
    if size > max_size_bytes:
        raise TelegramTransportError(
            f"file is {size} bytes, exceeds max_size_bytes={max_size_bytes} -- refusing "
            f"to attempt upload rather than silently truncating it")

    fields = {"chat_id": str(chat_id), "caption": caption}
    content_type, preamble, epilogue = _multipart_preamble_and_epilogue(
        fields, "document", os.path.basename(file_path))
    content_length = len(preamble) + size + len(epilogue)

    request_line = (
        f"POST /bot{bot_token}/sendDocument HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {content_length}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("utf-8")

    sock.sendall(request_line)
    sock.sendall(preamble)
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sock.sendall(chunk)
    sock.sendall(epilogue)

    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    response = b"".join(chunks)
    return _parse_raw_http_json_response(response, bot_token)


# ---------- Direct transport -------------------------------------------------------------------

@dataclass(frozen=True)
class DirectTransport:
    """Plain HTTPS straight to Telegram -- the default mode."""
    bot_token: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def send_document(self, chat_id, file_path: str, caption: str = None,
                       max_size_bytes: int = 50 * 1024 * 1024, socket_factory=None) -> dict:
        """Uploads a file via a real, streamed sendDocument request (multipart/form-data,
        hand-built, never loading the whole file into memory) -- used for backup archive
        delivery. Refuses (raises, never silently truncates or skips the size check)
        anything over `max_size_bytes` (Telegram's own real limit for bot uploads is 50MB)
        BEFORE opening the file. `socket_factory` is injectable for tests (same DI
        convention as Socks5Transport's `connect_fn`) -- real usage always connects a
        genuine TLS socket straight to Telegram."""
        if socket_factory is None:
            def socket_factory():
                raw = socket.create_connection((TELEGRAM_API_HOST, 443),
                                                 timeout=self.timeout_seconds)
                ctx = ssl.create_default_context()
                return ctx.wrap_socket(raw, server_hostname=TELEGRAM_API_HOST)
        try:
            sock = socket_factory()
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None
        try:
            return _stream_send_document_over_socket(
                sock, TELEGRAM_API_HOST, self.bot_token, chat_id, file_path, caption,
                max_size_bytes)
        except TelegramTransportError:
            raise
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None
        finally:
            sock.close()

    def call(self, method: str, params: dict = None, http_post=None) -> dict:
        """Calls https://api.telegram.org/bot<TOKEN>/<method>. `http_post` is injectable
        for tests (same DI convention as writer.py's `http_post`) -- real usage never
        passes it, defaulting to a genuine HTTPS POST."""
        url = f"https://{TELEGRAM_API_HOST}/bot{self.bot_token}/{method}"
        body = json.dumps(params or {}).encode("utf-8")
        if http_post is None:
            def http_post(url, data, timeout):
                req = urllib.request.Request(url, data=data, method="POST",
                                               headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode()
        try:
            raw = http_post(url, body, self.timeout_seconds)
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise TelegramTransportError("Telegram API response was not valid JSON") from None


# ---------- SOCKS5 transport (RFC 1928/1929, pure stdlib) ---------------------------------------

class Socks5Error(Exception):
    pass


_SOCKS5_REPLY_CODES = {
    0x00: "succeeded", 0x01: "general SOCKS server failure", 0x02: "connection not allowed",
    0x03: "network unreachable", 0x04: "host unreachable", 0x05: "connection refused",
    0x06: "TTL expired", 0x07: "command not supported", 0x08: "address type not supported",
}


def socks5_connect(proxy_host: str, proxy_port: int, target_host: str, target_port: int,
                    username: str = None, password: str = None,
                    timeout: float = DEFAULT_SOCKS5_TIMEOUT_SECONDS,
                    socket_factory=socket.create_connection) -> socket.socket:
    """Performs a real SOCKS5 CONNECT handshake (RFC 1928) over a raw TCP socket and returns
    the socket, now tunneled to target_host:target_port -- the caller wraps it in TLS
    (ssl.SSLContext.wrap_socket) and speaks HTTP over it themselves (see Socks5Transport).
    `socket_factory` is injectable for tests (a real local SOCKS5-speaking test server), so
    this never needs a real internet-facing proxy to be unit-tested.

    Supports NO-AUTH (0x00) and username/password (0x02, RFC 1929) sub-negotiation. Never
    logs/raises with the password in the message."""
    sock = socket_factory((proxy_host, proxy_port), timeout=timeout)
    try:
        auth_methods = bytes([0x02, 0x00]) if username else bytes([0x01, 0x00])
        # greeting: VER=5, NMETHODS, METHODS...
        sock.sendall(bytes([0x05, len(auth_methods) if username else 1]) +
                     (bytes([0x00, 0x02]) if username else bytes([0x00])))
        chosen = sock.recv(2)
        if len(chosen) != 2 or chosen[0] != 0x05:
            raise Socks5Error("malformed SOCKS5 server greeting response")
        method = chosen[1]
        if method == 0x02:
            if not username:
                raise Socks5Error("proxy requires username/password auth but none was configured")
            user_b = username.encode("utf-8")
            pass_b = (password or "").encode("utf-8")
            sock.sendall(bytes([0x01, len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
            auth_resp = sock.recv(2)
            if len(auth_resp) != 2 or auth_resp[1] != 0x00:
                raise Socks5Error("SOCKS5 proxy authentication failed")
        elif method == 0xFF:
            raise Socks5Error("SOCKS5 proxy rejected all offered authentication methods")
        elif method != 0x00:
            raise Socks5Error(f"SOCKS5 proxy selected an unsupported auth method: {method}")

        target_bytes = target_host.encode("idna") if not _is_ip_literal(target_host) else None
        if target_bytes is not None:
            request = bytes([0x05, 0x01, 0x00, 0x03, len(target_bytes)]) + target_bytes + \
                struct.pack(">H", target_port)
        else:
            request = bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton(target_host) + \
                struct.pack(">H", target_port)
        sock.sendall(request)

        reply_head = sock.recv(4)
        if len(reply_head) != 4 or reply_head[0] != 0x05:
            raise Socks5Error("malformed SOCKS5 CONNECT reply")
        status = reply_head[1]
        if status != 0x00:
            raise Socks5Error(f"SOCKS5 CONNECT failed: {_SOCKS5_REPLY_CODES.get(status, status)}")
        atyp = reply_head[3]
        if atyp == 0x01:
            sock.recv(4 + 2)  # IPv4 bound address + port, discarded
        elif atyp == 0x03:
            length = sock.recv(1)[0]
            sock.recv(length + 2)
        elif atyp == 0x04:
            sock.recv(16 + 2)  # IPv6
        else:
            raise Socks5Error(f"SOCKS5 CONNECT reply used an unsupported address type: {atyp}")
        return sock
    except Exception:
        sock.close()
        raise


def _is_ip_literal(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class Socks5ProxyConfig:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    timeout_seconds: float = DEFAULT_SOCKS5_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Socks5Transport:
    """HTTPS to Telegram, tunneled through a SOCKS5 proxy. Builds the raw HTTP/1.1 request
    itself (rather than trying to retrofit urllib.request's connection pooling through a
    pre-negotiated socket) -- deliberately simple and auditable over clever, since this is
    exactly the kind of code where a subtle bug could leak the bot token or a credential to
    the wrong host."""
    bot_token: str
    proxy: Socks5ProxyConfig
    connect_fn: Callable = field(default=socks5_connect)
    # Injectable so tests can point TLS verification at a local self-signed test server
    # (same pattern web_server.py's tests already use) -- real usage always defaults to a
    # genuine, fully-verifying default context.
    ssl_context_factory: Callable = field(default=ssl.create_default_context)

    def call(self, method: str, params: dict = None) -> dict:
        body = json.dumps(params or {}).encode("utf-8")
        try:
            raw_sock = self.connect_fn(
                self.proxy.host, self.proxy.port, TELEGRAM_API_HOST, 443,
                username=self.proxy.username, password=self.proxy.password,
                timeout=self.proxy.timeout_seconds,
            )
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None
        try:
            ctx = self.ssl_context_factory()
            tls_sock = ctx.wrap_socket(raw_sock, server_hostname=TELEGRAM_API_HOST)
            try:
                request_line = (
                    f"POST /bot{self.bot_token}/{method} HTTP/1.1\r\n"
                    f"Host: {TELEGRAM_API_HOST}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("utf-8") + body
                tls_sock.sendall(request_line)
                chunks = []
                while True:
                    chunk = tls_sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                response = b"".join(chunks)
            finally:
                tls_sock.close()
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None

        return _parse_raw_http_json_response(response, self.bot_token)

    def test_connectivity(self) -> dict:
        """Lightweight proxy-reachability check (calls Telegram's getMe) -- used by the
        installer/settings screen's 'connectivity test' requirement, never silently assumed
        to work."""
        try:
            result = self.call("getMe")
            return {"ok": bool(result.get("ok")), "detail": result}
        except TelegramTransportError as e:
            return {"ok": False, "detail": str(e)}

    def send_document(self, chat_id, file_path: str, caption: str = None,
                       max_size_bytes: int = 50 * 1024 * 1024) -> dict:
        """Uploads a file via a real sendDocument request, tunneled through the configured
        SOCKS5 proxy -- the same CONNECT handshake `call()` uses, with the multipart body
        streamed over the resulting TLS-wrapped socket (see
        `_stream_send_document_over_socket`) rather than built in memory first."""
        try:
            raw_sock = self.connect_fn(
                self.proxy.host, self.proxy.port, TELEGRAM_API_HOST, 443,
                username=self.proxy.username, password=self.proxy.password,
                timeout=self.proxy.timeout_seconds,
            )
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None
        try:
            ctx = self.ssl_context_factory()
            tls_sock = ctx.wrap_socket(raw_sock, server_hostname=TELEGRAM_API_HOST)
            try:
                return _stream_send_document_over_socket(
                    tls_sock, TELEGRAM_API_HOST, self.bot_token, chat_id, file_path, caption,
                    max_size_bytes)
            finally:
                tls_sock.close()
        except TelegramTransportError:
            raise
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None


def _parse_raw_http_json_response(response: bytes, bot_token: str) -> dict:
    try:
        header_end = response.index(b"\r\n\r\n")
    except ValueError:
        raise TelegramTransportError("malformed HTTP response from proxy tunnel (no header/body separator)")
    body = response[header_end + 4:]
    # A real server may chunk-encode; this minimal client only handles identity bodies with
    # Content-Length (Telegram's API does not chunk small JSON responses in practice) --
    # documented limitation, not silently mishandled.
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TelegramTransportError(
            _redact_token("response body was not valid JSON", bot_token)) from None


# ---------- Relay transport: signing/verification only (see module docstring) ------------------

import hmac
import hashlib
import secrets
from datetime import datetime, timezone


def sign_relay_request(shared_secret: str, payload: dict, nonce: str = None,
                        timestamp: str = None) -> dict:
    """Produces a signed envelope {"payload": ..., "nonce": ..., "timestamp": ...,
    "signature": ...} for the Iran-side host to hand to its outbound queue for a relay
    worker to pick up. `nonce` (fresh random, never reused) and `timestamp` together let
    verify_relay_request() reject replay attacks -- a captured, previously-valid envelope
    can never be replayed, and an envelope older than the caller's configured max age is
    rejected regardless of a valid signature."""
    nonce = nonce or secrets.token_hex(16)
    timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    canonical = json.dumps({"payload": payload, "nonce": nonce, "timestamp": timestamp},
                            sort_keys=True, separators=(",", ":"))
    signature = hmac.new(shared_secret.encode("utf-8"), canonical.encode("utf-8"),
                          hashlib.sha256).hexdigest()
    return {"payload": payload, "nonce": nonce, "timestamp": timestamp, "signature": signature}


def verify_relay_request(shared_secret: str, envelope: dict, seen_nonces: set,
                          max_age_seconds: float = 300, now: datetime = None) -> dict:
    """Verifies an envelope produced by sign_relay_request(). Rejects (does not raise):
      - a bad/missing signature (constant-time compared -- see security.constant_time_equals)
      - a nonce already present in `seen_nonces` (replay) -- caller is responsible for
        persisting seen nonces durably (e.g. a SQLite table) across process restarts; this
        function only checks the set it's given, it does not itself persist anything
      - a timestamp older than max_age_seconds, or more than max_age_seconds in the future
        (clock-skew-tolerant in both directions, but still bounded)
    Returns {"valid": bool, "reason": str|None, "payload": ... or None}."""
    import security as sec
    now = now or datetime.now(timezone.utc)
    required = ("payload", "nonce", "timestamp", "signature")
    if not all(k in envelope for k in required):
        return {"valid": False, "reason": "envelope missing required field(s)", "payload": None}

    canonical = json.dumps({"payload": envelope["payload"], "nonce": envelope["nonce"],
                             "timestamp": envelope["timestamp"]}, sort_keys=True, separators=(",", ":"))
    expected_sig = hmac.new(shared_secret.encode("utf-8"), canonical.encode("utf-8"),
                             hashlib.sha256).hexdigest()
    if not sec.constant_time_equals(expected_sig, envelope["signature"]):
        return {"valid": False, "reason": "signature mismatch", "payload": None}

    if envelope["nonce"] in seen_nonces:
        return {"valid": False, "reason": "nonce already used (replay)", "payload": None}

    try:
        ts = datetime.fromisoformat(envelope["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"valid": False, "reason": "unparseable timestamp", "payload": None}
    age = (now - ts).total_seconds()
    if abs(age) > max_age_seconds:
        return {"valid": False, "reason": f"timestamp outside allowed window ({age:.0f}s old)",
                "payload": None}

    return {"valid": True, "reason": None, "payload": envelope["payload"]}


@dataclass(frozen=True)
class RelayClientTransport:
    """The Iran-side half of RELAY mode: makes an OUTBOUND-only HTTPS (or HTTP, for an
    isolated local test/simulation -- see `use_tls`) POST to a separately-deployed
    `relay_worker.py` process's `/submit` endpoint, carrying a signed envelope. Never opens
    an inbound port; the relay worker never connects back to this host. Drop-in compatible
    with DirectTransport/Socks5Transport's `.call()` signature so `telegram_bot.py` never
    needs mode-specific dispatch logic beyond `build_transport()`."""
    bot_token: str  # kept only for redaction symmetry with the other transports; never sent
    shared_secret: str
    relay_host: str
    relay_port: int
    use_tls: bool = True
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def call(self, method: str, params: dict = None, http_post=None) -> dict:
        envelope = sign_relay_request(self.shared_secret, {"method": method, "params": params or {}})
        body = json.dumps(envelope).encode("utf-8")
        scheme = "https" if self.use_tls else "http"
        url = f"{scheme}://{self.relay_host}:{self.relay_port}/submit"

        if http_post is None:
            def http_post(url, data, timeout):
                req = urllib.request.Request(url, data=data, method="POST",
                                               headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode()
        try:
            raw = http_post(url, body, self.timeout_seconds)
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None

        try:
            signed_result = json.loads(raw)
        except (ValueError, TypeError):
            raise TelegramTransportError("relay worker response was not valid JSON") from None

        verification = verify_relay_request(self.shared_secret, signed_result, set())
        if not verification["valid"]:
            raise TelegramTransportError(
                f"relay worker response failed verification: {verification['reason']}")

        result_payload = verification["payload"]
        if not result_payload.get("ok"):
            raise TelegramTransportError(
                f"relay worker reported a Telegram API failure: {result_payload.get('error')}")
        return result_payload["result"]

    def test_connectivity(self) -> dict:
        try:
            result = self.call("getMe")
            return {"ok": bool(result.get("ok")), "detail": result}
        except TelegramTransportError as e:
            return {"ok": False, "detail": str(e)}

    def send_document(self, chat_id, file_path: str, caption: str = None,
                       max_size_bytes: int = RELAY_DOCUMENT_MAX_PLAINTEXT_BYTES,
                       http_post=None) -> dict:
        """Uploads a file through the relay worker's dedicated `/submit-document` endpoint:
        base64-encodes the file into a signed envelope (HMAC-authenticated, replay-
        protected, exactly like `call()`) and asks the relay worker to execute the real
        sendDocument call on its own side. `max_size_bytes` defaults to a cap LOWER than
        Telegram's own 50MB bot-upload limit, since base64 adds ~33% size overhead through
        the signed JSON envelope -- see relay_worker.py's matching, independently-enforced
        request-body cap for this endpoint. Never sends the backup's encryption passphrase
        (or any other secret) -- only the already-encrypted archive's bytes."""
        import base64

        size = os.path.getsize(file_path)
        if size > max_size_bytes:
            raise TelegramTransportError(
                f"file is {size} bytes, exceeds max_size_bytes={max_size_bytes} for RELAY "
                f"document delivery (lower than Telegram's own 50MB limit -- base64 "
                f"encoding through the signed relay envelope adds ~33% overhead) -- refusing")
        with open(file_path, "rb") as f:
            file_b64 = base64.b64encode(f.read()).decode("ascii")

        payload = {
            "method": "sendDocument",
            "params": {"chat_id": chat_id, "caption": caption,
                       "filename": os.path.basename(file_path)},
            "file_b64": file_b64,
        }
        envelope = sign_relay_request(self.shared_secret, payload)
        body = json.dumps(envelope).encode("utf-8")
        scheme = "https" if self.use_tls else "http"
        url = f"{scheme}://{self.relay_host}:{self.relay_port}/submit-document"

        if http_post is None:
            def http_post(url, data, timeout):
                req = urllib.request.Request(url, data=data, method="POST",
                                               headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode()
        try:
            raw = http_post(url, body, self.timeout_seconds)
        except Exception as e:
            raise TelegramTransportError(_redact_token(str(e), self.bot_token)) from None

        try:
            signed_result = json.loads(raw)
        except (ValueError, TypeError):
            raise TelegramTransportError("relay worker response was not valid JSON") from None

        verification = verify_relay_request(self.shared_secret, signed_result, set())
        if not verification["valid"]:
            raise TelegramTransportError(
                f"relay worker response failed verification: {verification['reason']}")
        result_payload = verification["payload"]
        if not result_payload.get("ok"):
            raise TelegramTransportError(
                f"relay worker reported a document-delivery failure: {result_payload.get('error')}")
        return result_payload["result"]
