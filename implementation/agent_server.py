"""The hypervisor-side remote management agent (v1.8.0 Part 14). Runs ON the Virtualizor
host (local install mode is unchanged and remains the default everywhere else in this
project) and exposes a minimal, authenticated, DELIBERATELY READ-ONLY query surface so a
separate central management server can see this host's status WITHOUT ever being handed
this host's Virtualizor API credentials -- those never leave this host, exactly as today.

PROTOCOL: a central management server sends a signed envelope (same generic HMAC
sign/verify primitives already built for relay mode -- `telegram_transport.
sign_relay_request`/`verify_relay_request`; genuinely reusable since they were never
Telegram-specific) to this agent's `/query` endpoint. The agent verifies signature +
durable replay protection + timestamp freshness, executes ONE of a small allowlist of
read-only queries against `control_center_api.py` (never re-deriving business logic, never
touching the Virtualizor API directly), signs the result, and returns it synchronously.

SCOPE: deliberately read-only for this pass -- no renewal-apply, no emergency-grant
mutation, no credential rotation is reachable via this protocol at all. This matches the
project's established "safe read-only defaults" discipline and keeps the blast radius of a
compromised or misconfigured management-server credential to "can read this host's
already-non-secret status," never "can act on this host."

LIMITATION (honestly documented): this module provides AUTHENTICITY and INTEGRITY (HMAC)
but not transport CONFIDENTIALITY -- it speaks plain HTTP. An operator exposing
`bind_host` beyond 127.0.0.1 must put a TLS-terminating reverse proxy or VPN in front, same
caveat as already documented for web_server.py and relay_worker.py.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sqlite3
import threading
from pathlib import Path

ALLOWED_QUERIES = ("snapshot", "host", "network", "inventory", "renewals", "health")


def _json_safe(obj):
    """Recursively converts Decimal (used throughout control_center_api.py's returned
    snapshots for exact bandwidth/quota arithmetic) to str so the result can be both
    JSON-serialized AND HMAC-signed via sign_relay_request's own canonical json.dumps,
    which has no custom encoder hook. Never loses precision -- str(Decimal(...)) round
    trips exactly, unlike float()."""
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


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


class AgentApp:
    """Pure, socket-free core -- exactly the pattern established by web_server.py's WebApp
    and relay_worker.py's RelayWorkerApp."""

    def __init__(self, config_dir: str, state_dir: str, shared_secret: str, nonce_db_path: str,
                 max_envelope_age_seconds: float = 300):
        self.config_dir = config_dir
        self.state_dir = state_dir
        self.shared_secret = shared_secret
        self.nonce_db_path = nonce_db_path
        self.max_envelope_age_seconds = max_envelope_age_seconds

    def _run_query(self, query: str) -> dict:
        import control_center_api as api
        if query == "snapshot":
            return api.get_full_snapshot(self.config_dir, self.state_dir)
        if query == "host":
            return api.get_host_snapshot()
        if query == "network":
            return api.get_network_overview(self.config_dir, self.state_dir)
        if query == "inventory":
            return api.get_vps_inventory_overview(self.state_dir)
        if query == "renewals":
            return api.get_renewal_overview(self.config_dir, self.state_dir)
        if query == "health":
            import menu as menu_mod
            statuses = menu_mod.collect_timer_status()
            return {"timers": {unit: {**info, "health": menu_mod.classify_timer_health(
                        info["active"], info["enabled"])}
                    for unit, info in statuses.items()}}
        raise ValueError(f"unknown query: {query!r}")

    def handle_request(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        import telegram_transport as tt  # generic signed-envelope primitives, reused as-is

        if method != "POST" or path != "/query":
            return 404, b'{"error":"not found"}'

        try:
            envelope = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, b'{"error":"malformed JSON body"}'

        conn = open_seen_nonces_db(self.nonce_db_path)
        try:
            seen = _load_seen_nonces(conn)
            verification = tt.verify_relay_request(
                self.shared_secret, envelope, seen, max_age_seconds=self.max_envelope_age_seconds)
            if not verification["valid"]:
                return 403, json.dumps({"error": verification["reason"]}).encode("utf-8")
            _record_nonce(conn, envelope["nonce"])
        finally:
            conn.close()

        payload = verification["payload"]
        query = payload.get("query")
        if query not in ALLOWED_QUERIES:
            result_payload = {"ok": False, "error": f"query not allowed: {query!r}"}
        else:
            try:
                result_payload = {"ok": True, "result": _json_safe(self._run_query(query))}
            except Exception as e:  # noqa: BLE001 -- never crash the agent on one bad query
                result_payload = {"ok": False, "error": str(e)}

        signed_result = tt.sign_relay_request(self.shared_secret, result_payload)
        return 200, json.dumps(signed_result).encode("utf-8")


MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # query envelopes are small signed JSON, never this large


def _build_handler_class(app: AgentApp):
    class AgentRequestHandler(http.server.BaseHTTPRequestHandler):
        def _dispatch(self, method):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_REQUEST_BODY_BYTES:
                    self.send_response(413)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self.rfile.read(length) if length else b""
                status, resp_body = app.handle_request(method, self.path, body)
            except Exception as e:  # noqa: BLE001 -- never let one bad request crash the agent
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

        def log_message(self, fmt, *args):  # noqa: A003
            pass

    return AgentRequestHandler


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(config_dir: str, state_dir: str, shared_secret: str, nonce_db_path: str,
                  bind_host: str, bind_port: int,
                  max_envelope_age_seconds: float = 300) -> _ThreadingHTTPServer:
    app = AgentApp(config_dir, state_dir, shared_secret, nonce_db_path, max_envelope_age_seconds)
    handler_cls = _build_handler_class(app)
    return _ThreadingHTTPServer((bind_host, bind_port), handler_cls)


def run_server_in_thread(config_dir: str, state_dir: str, shared_secret: str, nonce_db_path: str,
                          bind_host: str, bind_port: int, max_envelope_age_seconds: float = 300):
    server = build_server(config_dir, state_dir, shared_secret, nonce_db_path, bind_host,
                           bind_port, max_envelope_age_seconds)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def query_agent(agent_host: str, agent_port: int, shared_secret: str, query: str,
                 use_tls: bool = True, timeout_seconds: int = 15, http_post=None) -> dict:
    """The management-server-side client: signs a query envelope, POSTs it to a remote
    agent's `/query`, verifies the signed response, and returns the inner result dict.
    Raises ValueError on any verification failure or reported error -- never silently
    returns a result it could not authenticate."""
    import json as _json
    import urllib.request
    import telegram_transport as tt

    if query not in ALLOWED_QUERIES:
        raise ValueError(f"unknown query: {query!r} (allowed: {ALLOWED_QUERIES})")

    envelope = tt.sign_relay_request(shared_secret, {"query": query})
    body = _json.dumps(envelope).encode("utf-8")
    scheme = "https" if use_tls else "http"
    url = f"{scheme}://{agent_host}:{agent_port}/query"

    if http_post is None:
        def http_post(url, data, timeout):
            req = urllib.request.Request(url, data=data, method="POST",
                                           headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()

    raw = http_post(url, body, timeout_seconds)
    signed_result = _json.loads(raw)
    verification = tt.verify_relay_request(shared_secret, signed_result, set())
    if not verification["valid"]:
        raise ValueError(f"agent response failed verification: {verification['reason']}")
    result_payload = verification["payload"]
    if not result_payload.get("ok"):
        raise ValueError(f"agent reported an error: {result_payload.get('error')}")
    return result_payload["result"]
