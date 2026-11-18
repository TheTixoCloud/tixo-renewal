"""Tests for web_server.py (v1.8.0 Part 11): the Web Control Center. Most tests call
WebApp.handle_request() directly (the pure, socket-free core); a handful spin up a REAL
server on an ephemeral localhost port to confirm the socket layer genuinely works end to end.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import web_auth as wa
import web_server as ws


def _dirs(tmp_path):
    config_dir = str(tmp_path / "config")
    state_dir = str(tmp_path / "state")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    return config_dir, state_dir


def _app_with_admin(tmp_path, username="admin", password="pw123456"):
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, username, password, role=wa.ROLE_ADMIN)
    return ws.WebApp(config_dir, state_dir), config_dir, state_dir


def _login(app, username="admin", password="pw123456"):
    body = f"username={username}&password={password}".encode()
    r = app.handle_request("POST", "/login", {}, {}, body, "127.0.0.1")
    assert r.status == 302
    cookie_header = r.headers["Set-Cookie"]
    token = cookie_header.split("=", 1)[1].split(";")[0]
    return {ws.SESSION_COOKIE_NAME: token}, token


# ---------- authentication / session gating -----------------------------------------------

def test_unauthenticated_request_redirects_to_login(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    r = app.handle_request("GET", "/", {}, {}, b"", "127.0.0.1")
    assert r.status == 302
    assert r.headers["Location"] == "/login"


def test_login_page_renders_without_auth(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    r = app.handle_request("GET", "/login", {}, {}, b"", "127.0.0.1")
    assert r.status == 200
    assert b"username" in r.body


def test_login_success_sets_cookie_and_redirects(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    assert token
    r = app.handle_request("GET", "/", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200


def test_login_wrong_password_returns_401_no_cookie(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    body = b"username=admin&password=WRONG"
    r = app.handle_request("POST", "/login", {}, {}, body, "127.0.0.1")
    assert r.status == 401
    assert "Set-Cookie" not in r.headers


def test_logout_destroys_session(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    r = app.handle_request("POST", "/logout", {}, cookies, b"", "127.0.0.1")
    assert r.status == 302
    r2 = app.handle_request("GET", "/", {}, cookies, b"", "127.0.0.1")
    assert r2.status == 302
    assert r2.headers["Location"] == "/login"


def test_login_rate_limited_after_threshold(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    body = b"username=admin&password=WRONG"
    for _ in range(wa.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
        app.handle_request("POST", "/login", {}, {}, body, "9.9.9.9")
    r = app.handle_request("POST", "/login", {}, {}, body, "9.9.9.9")
    assert r.status == 429


def test_unknown_route_returns_404(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", "/nonexistent-page", {}, cookies, b"", "127.0.0.1")
    assert r.status == 404


# ---------- all 16 real pages render for a logged-in user ------------------------------------

ALL_PAGES = ["/", "/host", "/network", "/traffic", "/inventory", "/renewals", "/emergency",
             "/review", "/alerts", "/reports", "/telegram", "/backups", "/settings",
             "/users", "/audit", "/health"]


@pytest.mark.parametrize("path", ALL_PAGES)
def test_each_page_renders_200_for_logged_in_user(tmp_path, path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", path, {}, cookies, b"", "127.0.0.1")
    assert r.status == 200
    assert b"TixoCloud" in r.body


def test_vps_detail_page_for_nonexistent_vps_handled_honestly(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", "/vps/9999", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200
    assert b"No such VPS" in r.body


def test_vps_detail_page_invalid_id_returns_404(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", "/vps/not-a-number", {}, cookies, b"", "127.0.0.1")
    assert r.status == 404


def test_vps_detail_shows_real_inventory_record(tmp_path):
    app, config_dir, state_dir = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    import inventory as inv
    from decimal import Decimal
    conn = inv.open_db(os.path.join(state_dir, "inventory.sqlite3"))
    parse_result = {
        "complete": True,
        "records": [{"vpsid": 42, "hostname": "testhost", "status": "unknown",
                      "quota_gb": Decimal("100"), "used_gb": Decimal("50"), "ram_mb": "1024",
                      "cores": "1", "disk_gb": "20", "virtualization": "kvm",
                      "locked": "unavailable", "node_serid": "unavailable",
                      "ip_primary": "unavailable", "ip_count": "unavailable",
                      "ip_list": "unavailable", "runtime_status": "running",
                      "network_suspended_reason": None}],
        "skipped_count": 0, "error": None, "authoritative_total": 1,
    }
    inv.ingest_snapshot(conn, "self", parse_result)
    conn.close()
    r = app.handle_request("GET", "/vps/42", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200
    assert b"testhost" in r.body


# ---------- CSRF enforcement -----------------------------------------------------------------

def test_post_without_csrf_token_rejected(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    body = b"mode=manual_review"
    r = app.handle_request("POST", "/emergency", {}, cookies, body, "127.0.0.1")
    assert r.status == 403


def test_post_with_wrong_csrf_token_rejected(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    body = b"mode=manual_review&csrf_token=totally-wrong-token"
    r = app.handle_request("POST", "/emergency", {}, cookies, body, "127.0.0.1")
    assert r.status == 403


def test_post_with_correct_csrf_token_succeeds(tmp_path):
    app, config_dir, _ = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"mode=manual_review&csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/emergency", {}, cookies, body, "127.0.0.1")
    assert r.status == 200
    assert b"Saved" in r.body
    import emergency_policy as emp
    assert emp.load_policy_settings(config_dir).mode == emp.MODE_MANUAL_REVIEW


# ---------- RBAC: viewer cannot perform admin (mutating) actions -----------------------------

def test_viewer_cannot_change_emergency_policy_mode(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, "viewer1", "pw123456", role=wa.ROLE_VIEWER)
    app = ws.WebApp(config_dir, state_dir)
    cookies, token = _login(app, "viewer1", "pw123456")
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"mode=manual_review&csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/emergency", {}, cookies, body, "127.0.0.1")
    assert r.status == 403


def test_viewer_can_still_view_read_only_pages(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, "viewer1", "pw123456", role=wa.ROLE_VIEWER)
    app = ws.WebApp(config_dir, state_dir)
    cookies, _ = _login(app, "viewer1", "pw123456")
    r = app.handle_request("GET", "/emergency", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200


def test_viewer_cannot_create_users(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, "viewer1", "pw123456", role=wa.ROLE_VIEWER)
    app = ws.WebApp(config_dir, state_dir)
    cookies, token = _login(app, "viewer1", "pw123456")
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"form_action=create&username=newguy&password=pw&role=admin&csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/users", {}, cookies, body, "127.0.0.1")
    assert r.status == 403
    assert wa.get_user(config_dir, "newguy") is None


def test_admin_can_create_user_via_web(tmp_path):
    app, config_dir, _ = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"form_action=create&username=newviewer&password=pw123456&role=viewer&csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/users", {}, cookies, body, "127.0.0.1")
    assert r.status == 200
    assert wa.get_user(config_dir, "newviewer") is not None


# ---------- manual review actions are audited --------------------------------------------------

def test_manual_review_resolve_is_audited(tmp_path):
    app, config_dir, state_dir = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    import emergency_policy as emp
    policy = emp.ExhaustedBandwidthPolicy(mode=emp.MODE_MANUAL_REVIEW)
    emp.save_policy_settings(config_dir, policy)
    conn = emp.open_db(os.path.join(state_dir, "emergency_policy.sqlite3"))
    result = emp.evaluate_exhaustion(conn, 7, quota_gb="100", used_gb="150", policy=policy)
    conn.close()
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"review_id={result['review_id']}&action=resolve&notes=handled&csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/review", {}, cookies, body, "127.0.0.1")
    assert r.status == 200
    import audit_log
    audit_conn = audit_log.open_db(os.path.join(state_dir, "audit_log.sqlite3"))
    events = audit_log.list_recent_events(audit_conn)
    assert any(e["action"] == "manual_review_resolved" for e in events)


# ---------- never calls the Virtualizor API directly -------------------------------------------

def test_web_server_never_calls_virtualizor_api_for_overview(tmp_path, monkeypatch):
    import writer as writer_mod
    def boom(*a, **kw):
        raise AssertionError("web_server must never call the Virtualizor API directly")
    monkeypatch.setattr(writer_mod, "call_vs_api", boom, raising=False)
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", "/", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200


# ---------- localization / theme ----------------------------------------------------------------

def test_fa_language_cookie_renders_rtl(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    cookies[ws.LANG_COOKIE_NAME] = "fa"
    r = app.handle_request("GET", "/", {}, cookies, b"", "127.0.0.1")
    assert b'dir="rtl"' in r.body


def test_en_language_default_renders_ltr(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", "/", {}, cookies, b"", "127.0.0.1")
    assert b'dir="ltr"' in r.body


def test_light_theme_cookie_applied(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    cookies[ws.THEME_COOKIE_NAME] = "light"
    r = app.handle_request("GET", "/", {}, cookies, b"", "127.0.0.1")
    assert b'data-theme="light"' in r.body


# ---------- live API endpoint --------------------------------------------------------------

def test_api_live_requires_auth(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    r = app.handle_request("GET", "/api/live", {}, {}, b"", "127.0.0.1")
    assert r.status == 302


def test_api_live_reports_unconfigured_honestly(tmp_path):
    app, _, _ = _app_with_admin(tmp_path)
    cookies, _ = _login(app)
    r = app.handle_request("GET", "/api/live", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200
    data = json.loads(r.body)
    assert data["configured"] is False


# ---------- real end-to-end socket test ---------------------------------------------------------

def test_real_server_end_to_end_over_a_real_socket(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, "admin", "pw123456", role=wa.ROLE_ADMIN)
    server, thread = ws.run_server_in_thread(config_dir, state_dir, "127.0.0.1", 0)
    port = server.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/login")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert b"username" in resp.read()

        # unauthenticated request to a protected page should redirect (urllib follows by
        # default and will end up back at /login -- confirm final status 200 at /login)
        req2 = urllib.request.Request(f"http://127.0.0.1:{port}/")
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            assert resp2.status == 200
            assert resp2.url.endswith("/login")
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------- renewal_cli.py: web-run subcommand ---------------------------------------------

def test_cmd_web_run_blocked_when_disabled(tmp_path, capsys):
    import renewal_cli
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_web_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_web_run_starts_and_stops_with_max_iterations(tmp_path, capsys):
    import renewal_cli
    import web_config as wcfg
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    wcfg.save_web_settings(renewal_cli.CONFIG_DIR,
                            wcfg.WebSettings(enabled=True, bind_host="127.0.0.1", bind_port=18443))

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_web_run(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Starting Web Control Center" in out
    assert "stopped" in out.lower()


def test_menu_items_includes_web_control_center_config():
    import menu
    labels = [label for label, _ in menu.MENU_ITEMS]
    assert "Configure Web Control Center (optional)" in labels


# ---------- backups page: real archive creation, audit, RBAC -----------------------------------

def test_viewer_cannot_create_backup_via_web(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, "viewer1", "pw123456", role=wa.ROLE_VIEWER)
    app = ws.WebApp(config_dir, state_dir)
    cookies, token = _login(app, "viewer1", "pw123456")
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/backups", {}, cookies, body, "127.0.0.1")
    assert r.status == 403
    import backup as backup_mod
    assert backup_mod.list_backup_archives(app._backup_root()) == []


def test_admin_can_create_backup_via_web_and_it_is_audited(tmp_path):
    app, config_dir, state_dir = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"csrf_token={csrf}".encode()
    r = app.handle_request("POST", "/backups", {}, cookies, body, "127.0.0.1")
    assert r.status == 200
    assert b"Created:" in r.body

    import backup as backup_mod
    archives = backup_mod.list_backup_archives(app._backup_root())
    assert len(archives) == 1

    import audit_log
    conn = audit_log.open_db(os.path.join(state_dir, "audit_log.sqlite3"))
    events = audit_log.list_recent_events(conn)
    conn.close()
    assert any(e["action"] == "backup_created" for e in events)


def test_backups_page_lists_created_archives(tmp_path):
    app, config_dir, state_dir = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    import backup as backup_mod
    archive = backup_mod.create_full_backup_archive(state_dir, config_dir, app._backup_root(),
                                                       label="pre-existing")
    r = app.handle_request("GET", "/backups", {}, cookies, b"", "127.0.0.1")
    assert r.status == 200
    assert os.path.basename(archive).encode() in r.body


def test_backups_page_never_exposes_secret_files(tmp_path):
    app, config_dir, state_dir = _app_with_admin(tmp_path)
    cookies, token = _login(app)
    secret = wa.get_or_create_csrf_secret(config_dir)
    csrf = wa.derive_csrf_token(secret, token)
    body = f"csrf_token={csrf}".encode()
    app.handle_request("POST", "/backups", {}, cookies, body, "127.0.0.1")

    import backup as backup_mod
    import tarfile
    archives = backup_mod.list_backup_archives(app._backup_root())
    archive_path = os.path.join(app._backup_root(), archives[0])
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
    joined = "\n".join(names)
    assert "csrf_secret.key" not in joined
    assert "users.json" not in joined


# ---------- TLS (manual certificate paths) ------------------------------------------------------

def _generate_self_signed_cert(tmp_path):
    import subprocess
    cert_path = str(tmp_path / "cert.pem")
    key_path = str(tmp_path / "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key_path,
         "-out", cert_path, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
        check=True, capture_output=True)
    return cert_path, key_path


def test_build_server_with_valid_cert_wraps_socket_in_tls(tmp_path):
    cert_path, key_path = _generate_self_signed_cert(tmp_path)
    config_dir, state_dir = _dirs(tmp_path)
    server = ws.build_server(config_dir, state_dir, "127.0.0.1", 0,
                              tls_cert_path=cert_path, tls_key_path=key_path)
    try:
        import ssl
        assert isinstance(server.socket, ssl.SSLSocket)
    finally:
        server.server_close()


def test_build_server_rejects_cert_without_key(tmp_path):
    cert_path, _ = _generate_self_signed_cert(tmp_path)
    config_dir, state_dir = _dirs(tmp_path)
    with pytest.raises(ws.TlsCertificateError, match="both"):
        ws.build_server(config_dir, state_dir, "127.0.0.1", 0, tls_cert_path=cert_path)


def test_build_server_rejects_unreadable_cert_path(tmp_path):
    config_dir, state_dir = _dirs(tmp_path)
    with pytest.raises(ws.TlsCertificateError):
        ws.build_server(config_dir, state_dir, "127.0.0.1", 0,
                         tls_cert_path="/nonexistent/cert.pem",
                         tls_key_path="/nonexistent/key.pem")


def test_real_tls_server_end_to_end_over_a_real_socket(tmp_path):
    import ssl
    import urllib.request

    cert_path, key_path = _generate_self_signed_cert(tmp_path)
    config_dir, state_dir = _dirs(tmp_path)
    wa.create_user(config_dir, "admin", "pw123456", role=wa.ROLE_ADMIN)
    server, thread = ws.run_server_in_thread(config_dir, state_dir, "127.0.0.1", 0,
                                               tls_cert_path=cert_path, tls_key_path=key_path)
    try:
        port = server.server_address[1]
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed test cert -- not a trust assertion
        resp = urllib.request.urlopen(f"https://127.0.0.1:{port}/login", context=ctx, timeout=5)
        assert resp.status == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cmd_web_run_blocked_on_bad_tls_cert(tmp_path, capsys):
    import renewal_cli
    import web_config as wcfg
    renewal_cli.CONFIG_DIR = str(tmp_path / "config")
    renewal_cli.STATE_DIR = str(tmp_path / "state")
    os.makedirs(renewal_cli.CONFIG_DIR, exist_ok=True)
    os.makedirs(renewal_cli.STATE_DIR, exist_ok=True)
    wcfg.save_web_settings(renewal_cli.CONFIG_DIR,
                            wcfg.WebSettings(enabled=True, bind_host="127.0.0.1",
                                              bind_port=18444, tls_cert_path="/nonexistent/c.pem",
                                              tls_key_path="/nonexistent/k.pem"))

    class _Args:
        max_iterations = 1
    rc = renewal_cli.cmd_web_run(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out
