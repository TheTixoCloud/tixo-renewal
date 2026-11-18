"""The relay worker process (v1.8.0 Part 13) -- the SEPARATE program meant to run on a VPS
that CAN reach Telegram, for Iran-side hosts that cannot. Mirrors web_server.py's
architecture exactly: `RelayWorkerApp.handle_request()` is the pure, socket-free core that
the test suite exercises directly; `build_server()`/`run_server_in_thread()` are the thin
real-socket wrapper.

PROTOCOL: the Iran-side host (never reachable from outside, no inbound port needed) makes an
OUTBOUND HTTPS POST to this worker's `/submit` endpoint with a signed envelope (produced by
`telegram_transport.sign_relay_request`). This worker verifies the envelope (signature,
replay via a durable seen-nonces table, timestamp freshness), executes the requested
Telegram Bot API call directly, signs the result with the SAME shared secret, and returns it
synchronously in the HTTP response -- no second poll-for-result round trip needed, and the
relay worker never initiates a connection back to the Iran host.

A SEPARATE endpoint, `/submit-document`, handles encrypted backup-archive delivery: the
envelope's payload carries a base64-encoded file (never a plaintext passphrase or any other
secret) plus sendDocument params; this worker decodes it to a temporary file, calls the real
Telegram sendDocument API, and deletes the temp file immediately after. `/submit-document`
has its OWN, larger request-body cap (see MAX_DOCUMENT_REQUEST_BODY_BYTES) -- kept entirely
separate from `/submit`'s small-envelope cap so raising one can never accidentally weaken
the other.

SECRETS: the relay worker's own bot_token/shared_secret live in its own config_dir on its
own machine (see telegram_config.RelayWorkerSecrets) -- it never shares a filesystem or any
other secret with the Iran-side host.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sqlite3
import threading
from pathlib import Path


def open_seen_nonces_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS seen_nonces (nonce TEXT PRIMARY KEY, seen_at TEXT NOT NULL);"
    )
    return conn


def _load_seen_nonces(conn: sqlite3.Connection) -> set:
    return {row[0] for row in conn.execute("SELECT nonce FROM seen_nonces").fetchall()}


def _record_nonce(conn: sqlite3.Connection, nonce: str) -> None:
    from datetime import datetime, timezone
    conn.execute("INSERT OR IGNORE INTO seen_nonces (nonce, seen_at) VALUES (?, ?)",
                 (nonce, datetime.now(timezone.utc).isoformat()))


class RelayWorkerApp:
    """Pure, socket-free core. One instance per running worker process."""

    def __init__(self, bot_token: str, shared_secret: str, nonce_db_path: str,
                 max_envelope_age_seconds: float = 300, telegram_call=None,
                 telegram_send_document=None):
        self.bot_token = bot_token
        self.shared_secret = shared_secret
        self.nonce_db_path = nonce_db_path
        self.max_envelope_age_seconds = max_envelope_age_seconds
        # Both injectable for tests -- default to real DirectTransport calls against Telegram.
        self._telegram_call = telegram_call
        self._telegram_send_document = telegram_send_document

    def _call_telegram(self, method: str, params: dict) -> dict:
        if self._telegram_call is not None:
            return self._telegram_call(method, params)
        import telegram_transport as tt
        return tt.DirectTransport(bot_token=self.bot_token).call(method, params)

    def _send_telegram_document(self, chat_id, file_path: str, caption: str = None) -> dict:
        if self._telegram_send_document is not None:
            return self._telegram_send_document(chat_id, file_path, caption)
        import telegram_transport as tt
        return tt.DirectTransport(bot_token=self.bot_token).send_document(
            chat_id, file_path, caption=caption)

    def _verify_envelope(self, body: bytes):
        """Shared by both endpoints: parses + verifies a signed envelope. Returns
        (error_response_or_None, payload_or_None)."""
        import telegram_transport as tt
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return (400, b'{"error":"malformed JSON body"}'), None

        conn = open_seen_nonces_db(self.nonce_db_path)
        try:
            seen = _load_seen_nonces(conn)
            verification = tt.verify_relay_request(
                self.shared_secret, envelope, seen, max_age_seconds=self.max_envelope_age_seconds)
            if not verification["valid"]:
                return (403, json.dumps({"error": verification["reason"]}).encode("utf-8")), None
            _record_nonce(conn, envelope["nonce"])
        finally:
            conn.close()
        return None, verification["payload"]

    def handle_request(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        import telegram_transport as tt

        if method != "POST" or path not in ("/submit", "/submit-document"):
            return 404, b'{"error":"not found"}'

        error, payload = self._verify_envelope(body)
        if error is not None:
            return error

        if path == "/submit-document":
            return self._handle_document(payload)

        tg_method = payload.get("method")
        tg_params = payload.get("params") or {}
        if not tg_method:
            return 400, b'{"error":"payload missing method"}'

        try:
            tg_result = self._call_telegram(tg_method, tg_params)
            result_payload = {"ok": True, "result": tg_result}
        except tt.TelegramTransportError as e:
            result_payload = {"ok": False, "error": str(e)}

        signed_result = tt.sign_relay_request(self.shared_secret, result_payload)
        return 200, json.dumps(signed_result).encode("utf-8")

    def _handle_document(self, payload: dict) -> tuple[int, bytes]:
        import base64
        import tempfile
        import telegram_transport as tt

        file_b64 = payload.get("file_b64")
        params = payload.get("params") or {}
        if not file_b64:
            result_payload = {"ok": False, "error": "payload missing file_b64"}
        else:
            try:
                file_bytes = base64.b64decode(file_b64, validate=True)
            except Exception as e:
                result_payload = {"ok": False, "error": f"invalid base64 in payload: {e}"}
            else:
                if len(file_bytes) > MAX_DOCUMENT_PLAINTEXT_BYTES:
                    result_payload = {
                        "ok": False,
                        "error": f"decoded document is {len(file_bytes)} bytes, exceeds "
                                 f"MAX_DOCUMENT_PLAINTEXT_BYTES={MAX_DOCUMENT_PLAINTEXT_BYTES}"}
                else:
                    suffix = "-" + os.path.basename(params.get("filename") or "relay-document")
                    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                    try:
                        with os.fdopen(tmp_fd, "wb") as f:
                            f.write(file_bytes)
                        tg_result = self._send_telegram_document(
                            params.get("chat_id"), tmp_path, caption=params.get("caption"))
                        result_payload = {"ok": True, "result": tg_result}
                    except tt.TelegramTransportError as e:
                        result_payload = {"ok": False, "error": str(e)}
                    finally:
                        os.remove(tmp_path)

        signed_result = tt.sign_relay_request(self.shared_secret, result_payload)
        return 200, json.dumps(signed_result).encode("utf-8")


MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # /submit envelopes are small signed JSON, never this large
# /submit-document only: base64 of up to MAX_DOCUMENT_PLAINTEXT_BYTES plaintext (~33%
# overhead) plus JSON/signature overhead. Kept entirely separate from MAX_REQUEST_BODY_BYTES
# above so raising this one can never accidentally weaken the small-envelope cap.
MAX_DOCUMENT_PLAINTEXT_BYTES = 15 * 1024 * 1024
MAX_DOCUMENT_REQUEST_BODY_BYTES = 22 * 1024 * 1024


def _build_handler_class(app: RelayWorkerApp):
    class RelayWorkerHandler(http.server.BaseHTTPRequestHandler):
        def _dispatch(self, method):
            try:
                length = int(self.headers.get("Content-Length", 0))
                cap = (MAX_DOCUMENT_REQUEST_BODY_BYTES if self.path == "/submit-document"
                       else MAX_REQUEST_BODY_BYTES)
                if length > cap:
                    self.send_response(413)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self.rfile.read(length) if length else b""
                status, resp_body = app.handle_request(method, self.path, body)
            except Exception as e:  # noqa: BLE001 -- never let one bad request crash the worker
                status, resp_body = 500, json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        def do_POST(self):
            self._dispatch("POST")

        def do_GET(self):
            self._dispatch("GET")

        def log_message(self, fmt, *args):  # noqa: A003 -- suppress default stderr noise
            pass

    return RelayWorkerHandler


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(bot_token: str, shared_secret: str, nonce_db_path: str, bind_host: str,
                  bind_port: int, max_envelope_age_seconds: float = 300) -> _ThreadingHTTPServer:
    app = RelayWorkerApp(bot_token, shared_secret, nonce_db_path,
                          max_envelope_age_seconds=max_envelope_age_seconds)
    handler_cls = _build_handler_class(app)
    return _ThreadingHTTPServer((bind_host, bind_port), handler_cls)


def run_server_in_thread(bot_token: str, shared_secret: str, nonce_db_path: str, bind_host: str,
                          bind_port: int, max_envelope_age_seconds: float = 300):
    server = build_server(bot_token, shared_secret, nonce_db_path, bind_host, bind_port,
                           max_envelope_age_seconds)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
