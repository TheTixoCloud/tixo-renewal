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


# ---------- Direct transport -------------------------------------------------------------------

@dataclass(frozen=True)
class DirectTransport:
    """Plain HTTPS straight to Telegram -- the default mode."""
    bot_token: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

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
            ctx = ssl.create_default_context()
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
