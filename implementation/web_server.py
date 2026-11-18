"""TixoCloud Web Control Center -- stdlib-only HTTP server (v1.8.0 Part 11), zero third-party
dependency (no Flask/Django, matching this project's established convention). Built entirely
on the already-implemented, already-tested layers: `control_center_api.py` for data,
`web_auth.py` for sessions/CSRF/users, `emergency_policy.py`/`reports.py`/`host_time.py` for
the mutating admin actions it exposes.

ARCHITECTURE: `WebApp.handle_request()` is the pure, socket-free core -- given a parsed
request (method/path/headers/cookies/body/remote_addr), it returns a `WebResponse`
(status/headers/body). This is what the test suite calls directly for the large majority of
route tests. `TixoRequestHandler` (a thin `http.server.BaseHTTPRequestHandler` subclass) is
the only part that touches a real socket, translating a raw HTTP request into that same call
-- so the routing/auth/rendering logic is identical whether exercised via a direct function
call in a test or a real TCP connection in production.

SAFETY: binds to 127.0.0.1 by default (never a public interface without explicit operator
reconfiguration -- see web_config.py); the server does not even start unless
`WebSettings.enabled` is True (same pattern as the Telegram bot); read-only pages require
login, mutating actions additionally require the "admin" role AND a valid CSRF token; no
Virtualizor API call is ever made directly by this module (everything goes through
control_center_api.py, which already enforces that itself).
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from dataclasses import dataclass, field
from http import cookies as http_cookies

SESSION_COOKIE_NAME = "tixo_session"
# Caps every request body BEFORE reading it into memory -- this UI's own forms never
# approach this size; protects a busy hypervisor from a request claiming an enormous
# Content-Length, authenticated or not.
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
THEME_COOKIE_NAME = "tixo_theme"
LANG_COOKIE_NAME = "tixo_lang"

LABELS = {
    "en": {
        "overview": "Overview", "host": "Live Host Monitoring", "network": "Network Traffic",
        "traffic": "Traffic Analytics", "inventory": "VPS Inventory", "renewals": "Renewal Operations",
        "emergency": "Emergency Bandwidth", "review": "Manual Review", "alerts": "Alerts",
        "reports": "Reports", "telegram": "Telegram Settings", "backups": "Backups",
        "settings": "System Settings", "users": "User Management", "audit": "Audit Logs",
        "health": "Service Health", "logout": "Logout",
    },
    "fa": {
        "overview": "بررسی کلی", "host": "نظارت زنده میزبان", "network": "ترافیک شبکه",
        "traffic": "تحلیل ترافیک", "inventory": "فهرست VPS", "renewals": "عملیات تمدید",
        "emergency": "پهنای باند اضطراری", "review": "بازبینی دستی", "alerts": "هشدارها",
        "reports": "گزارش‌ها", "telegram": "تنظیمات تلگرام", "backups": "پشتیبان‌گیری",
        "settings": "تنظیمات سیستم", "users": "مدیریت کاربران", "audit": "گزارش رویدادها",
        "health": "سلامت سرویس", "logout": "خروج",
    },
}

NAV_ORDER = ["overview", "host", "network", "traffic", "inventory", "renewals", "emergency",
             "review", "alerts", "reports", "telegram", "backups", "settings", "users",
             "audit", "health"]
NAV_PATHS = {
    "overview": "/", "host": "/host", "network": "/network", "traffic": "/traffic",
    "inventory": "/inventory", "renewals": "/renewals", "emergency": "/emergency",
    "review": "/review", "alerts": "/alerts", "reports": "/reports", "telegram": "/telegram",
    "backups": "/backups", "settings": "/settings", "users": "/users", "audit": "/audit",
    "health": "/health",
}

CSS = """
:root[data-theme="dark"] { --bg:#0f1115; --fg:#e6e6e6; --card:#1a1d24; --accent:#e03131;
  --border:#2a2d35; --muted:#9aa0a6; }
:root[data-theme="light"] { --bg:#f7f7f8; --fg:#1a1a1a; --card:#ffffff; --accent:#c92a2a;
  --border:#dcdde0; --muted:#5a5f66; }
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  margin:0; display:flex; min-height:100vh; }
[dir="rtl"] body { direction: rtl; }
nav { width:220px; background:var(--card); border-right:1px solid var(--border); padding:16px; flex-shrink:0; }
[dir="rtl"] nav { border-right:none; border-left:1px solid var(--border); }
nav a { display:block; color:var(--fg); text-decoration:none; padding:8px 10px; border-radius:6px; margin-bottom:2px; font-size:14px; }
nav a:hover, nav a.active { background:var(--accent); color:white; }
main { flex:1; padding:24px; max-width:1000px; }
h1 { color:var(--accent); font-size:20px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:16px; }
table { width:100%; border-collapse: collapse; font-size:13px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }
[dir="rtl"] th, [dir="rtl"] td { text-align:right; }
.muted { color:var(--muted); font-size:12px; }
.badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:12px; }
.badge-ok { background:#2b8a3e; color:white; }
.badge-warn { background:#e8590c; color:white; }
.badge-off { background:var(--muted); color:white; }
input, select, button { padding:6px 10px; border-radius:6px; border:1px solid var(--border);
  background:var(--bg); color:var(--fg); }
button { background:var(--accent); color:white; cursor:pointer; border:none; }
form { margin-bottom:8px; }
.topbar { display:flex; justify-content:flex-end; gap:8px; padding:8px 16px; }
@media (max-width: 700px) { body { flex-direction:column; } nav { width:100%; } }
"""


@dataclass
class WebResponse:
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: bytes = b""
    headers: dict = field(default_factory=dict)


def _html_escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class WebApp:
    """The pure, socket-free core -- see module docstring."""

    def __init__(self, config_dir: str, state_dir: str, tls_enabled: bool = False):
        self.config_dir = config_dir
        self.state_dir = state_dir
        # When true, the session cookie is marked Secure (never sent over a future
        # plaintext connection) -- set by build_server() when a real TLS cert/key pair is
        # configured. Defaults to False so plain-HTTP/test usage is unaffected.
        self.tls_enabled = tls_enabled

    # ---------- shared plumbing -----------------------------------------------------------

    def _session_db_path(self) -> str:
        return os.path.join(self.state_dir, "web_sessions.sqlite3")

    def _rate_limit_db_path(self) -> str:
        return os.path.join(self.state_dir, "web_login_ratelimit.sqlite3")

    def _audit_db_path(self) -> str:
        return os.path.join(self.state_dir, "audit_log.sqlite3")

    def _current_user(self, cookies: dict):
        import web_auth as wa
        token = cookies.get(SESSION_COOKIE_NAME)
        if not token:
            return None, token
        conn = wa.open_session_db(self._session_db_path())
        try:
            user = wa.get_session_user(self.config_dir, conn, token)
        finally:
            conn.close()
        return user, token

    def _render_page(self, title: str, body_html: str, lang: str, theme: str,
                      active_nav: str = None) -> bytes:
        import web_config as wc
        import renewal_cli
        lang = lang if lang in wc.VALID_LANGUAGES else "en"
        theme = theme if theme in wc.VALID_THEMES else "dark"
        labels = LABELS[lang]
        direction = "rtl" if lang == "fa" else "ltr"
        nav_html = "".join(
            f'<a href="{NAV_PATHS[key]}" class="{"active" if key == active_nav else ""}">'
            f'{labels[key]}</a>' for key in NAV_ORDER)
        return f"""<!DOCTYPE html>
<html lang="{lang}" dir="{direction}" data-theme="{theme}">
<head><meta charset="utf-8"><title>TixoCloud -- {_html_escape(title)}</title>
<style>{CSS}</style></head>
<body>
<nav><h1>TixoCloud</h1>{nav_html}
<form method="post" action="/logout"><button type="submit">{labels['logout']}</button></form>
</nav>
<main><h1>{_html_escape(title)}</h1>{body_html}</main>
<footer class="muted">TixoCloud Control Center v{renewal_cli.VERSION}</footer>
</body></html>""".encode("utf-8")

    def _redirect(self, location: str, extra_headers: dict = None) -> WebResponse:
        headers = {"Location": location}
        if extra_headers:
            headers.update(extra_headers)
        return WebResponse(status=302, body=b"", headers=headers)

    def _forbidden(self, message: str = "Forbidden") -> WebResponse:
        return WebResponse(status=403, content_type="text/plain; charset=utf-8",
                            body=message.encode("utf-8"))

    def _not_found(self) -> WebResponse:
        return WebResponse(status=404, content_type="text/plain; charset=utf-8", body=b"Not Found")

    def _json(self, data: dict, status: int = 200) -> WebResponse:
        return WebResponse(status=status, content_type="application/json",
                            body=json.dumps(data).encode("utf-8"))

    def _audit(self, actor: str, action: str, detail: str = None, remote_addr: str = None):
        import audit_log
        conn = audit_log.open_db(self._audit_db_path())
        try:
            audit_log.record_event(conn, actor, action, detail=detail, remote_addr=remote_addr)
        finally:
            conn.close()

    # ---------- main dispatch --------------------------------------------------------------

    def handle_request(self, method: str, path: str, headers: dict, cookies: dict,
                        body: bytes, remote_addr: str) -> WebResponse:
        parsed = urllib.parse.urlsplit(path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        form = {}
        if method == "POST" and body:
            try:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8")).items()}
            except (UnicodeDecodeError, ValueError):
                form = {}

        lang = cookies.get(LANG_COOKIE_NAME, "en")
        theme = cookies.get(THEME_COOKIE_NAME, "dark")

        if route == "/login":
            return self._handle_login(method, form, cookies, remote_addr, lang, theme)
        if route == "/logout":
            return self._handle_logout(method, cookies)

        user, token = self._current_user(cookies)
        if user is None:
            return self._redirect("/login")

        if route == "/api/live" and method == "GET":
            return self._handle_api_live(query)

        routes = {
            "/": ("overview", self._page_overview),
            "/host": ("host", self._page_host),
            "/network": ("network", self._page_network),
            "/traffic": ("traffic", self._page_traffic),
            "/inventory": ("inventory", self._page_inventory),
            "/renewals": ("renewals", self._page_renewals),
            "/emergency": ("emergency", self._page_emergency),
            "/review": ("review", self._page_review),
            "/alerts": ("alerts", self._page_alerts),
            "/reports": ("reports", self._page_reports),
            "/telegram": ("telegram", self._page_telegram),
            "/backups": ("backups", self._page_backups),
            "/settings": ("settings", self._page_settings),
            "/users": ("users", self._page_users),
            "/audit": ("audit", self._page_audit),
            "/health": ("health", self._page_health),
        }
        if route.startswith("/vps/"):
            vpsid_raw = route[len("/vps/"):]
            return self._page_vps_detail(user, vpsid_raw, lang, theme)

        if route in routes:
            nav_key, handler = routes[route]
            return handler(user, method, form, token, remote_addr, lang, theme)

        return self._not_found()

    # ---------- auth -----------------------------------------------------------------------

    def _handle_login(self, method, form, cookies, remote_addr, lang, theme) -> WebResponse:
        import web_auth as wa
        import security as sec

        if method == "GET":
            body = f"""
<div class="card"><form method="post" action="/login">
<p><input name="username" placeholder="Username" required></p>
<p><input name="password" type="password" placeholder="Password" required></p>
<p><button type="submit">Login</button></p>
</form></div>"""
            return WebResponse(status=200, body=self._render_page("Login", body, lang, theme))

        rl_conn = sec.open_rate_limit_db(self._rate_limit_db_path())
        try:
            rl_result = wa.check_login_rate_limit(rl_conn, remote_addr or "unknown")
        finally:
            rl_conn.close()
        if not rl_result["allowed"]:
            self._audit("unknown", "login_rate_limited", remote_addr=remote_addr)
            return WebResponse(status=429, content_type="text/plain; charset=utf-8",
                                body=b"Too many login attempts -- try again later.")

        username = form.get("username", "")
        password = form.get("password", "")
        user = wa.verify_login(self.config_dir, username, password)
        if user is None:
            self._audit(username or "unknown", "login_failed", remote_addr=remote_addr)
            body = ('<div class="card"><p>Invalid credentials.</p>'
                    '<form method="post" action="/login">'
                    '<p><input name="username" placeholder="Username" required></p>'
                    '<p><input name="password" type="password" placeholder="Password" required></p>'
                    '<p><button type="submit">Login</button></p></form></div>')
            return WebResponse(status=401, body=self._render_page("Login", body, lang, theme))

        conn = wa.open_session_db(self._session_db_path())
        try:
            token = wa.create_session(conn, user.username)
        finally:
            conn.close()
        self._audit(user.username, "login_success", remote_addr=remote_addr)
        cookie = http_cookies.SimpleCookie()
        cookie[SESSION_COOKIE_NAME] = token
        cookie[SESSION_COOKIE_NAME]["path"] = "/"
        cookie[SESSION_COOKIE_NAME]["httponly"] = True
        if self.tls_enabled:
            cookie[SESSION_COOKIE_NAME]["secure"] = True
        return self._redirect("/", {"Set-Cookie": cookie[SESSION_COOKIE_NAME].OutputString()})

    def _handle_logout(self, method, cookies) -> WebResponse:
        import web_auth as wa
        if method != "POST":
            return self._redirect("/login")
        token = cookies.get(SESSION_COOKIE_NAME)
        if token:
            conn = wa.open_session_db(self._session_db_path())
            try:
                wa.destroy_session(conn, token)
            finally:
                conn.close()
        return self._redirect("/login")

    def _require_admin(self, user) -> bool:
        import web_auth as wa
        return user.role == wa.ROLE_ADMIN

    def _check_csrf(self, token: str, submitted: str) -> bool:
        import web_auth as wa
        secret = wa.get_or_create_csrf_secret(self.config_dir)
        expected = wa.derive_csrf_token(secret, token)
        return wa.verify_csrf_token(expected, submitted)

    def _csrf_field(self, token: str) -> str:
        import web_auth as wa
        secret = wa.get_or_create_csrf_secret(self.config_dir)
        value = wa.derive_csrf_token(secret, token)
        return f'<input type="hidden" name="csrf_token" value="{value}">'

    # ---------- live JSON API ---------------------------------------------------------------

    def _handle_api_live(self, query) -> WebResponse:
        import control_center_api as api
        import hostnet_sync
        settings = hostnet_sync.load_settings(self.config_dir)
        interface = settings.get("interface")
        if not interface:
            return self._json({"configured": False})
        live = api.get_live_network_reading(interface)
        return self._json({"configured": True, "interface": interface, **live})

    # ---------- pages (each: user, method, form, session_token, remote_addr, lang, theme) ---

    def _page_overview(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        snap = api.get_full_snapshot(self.config_dir, self.state_dir)
        fleet = snap["inventory"]["fleet"]
        net = snap["network"]
        ht = snap["host_time"]
        body = f"""
<div class="card"><b>Server time:</b> {_html_escape(ht.get('current_local'))}
 &nbsp; <b>Uptime:</b> {_html_escape(ht.get('uptime_human'))}</div>
<div class="card"><b>VPS:</b> {fleet['total_vps']} total
 (running {fleet['running_count']}, stopped {fleet['stopped_count']},
 suspended {fleet['suspended_count']}, unknown {fleet['unknown_status_count']})</div>
<div class="card"><b>Network:</b> {'configured on ' + _html_escape(net['interface']) if net.get('configured') else 'not configured'}</div>
<div class="card"><b>Write approved:</b> {snap['renewal'].get('write_approved')}
 &nbsp; <b>Auto-renewal:</b> {snap['renewal'].get('auto_renewal_enabled')}</div>
<div class="card"><b>Emergency policy mode:</b> {_html_escape(snap['emergency_policy']['mode'])}</div>
"""
        return WebResponse(body=self._render_page("Overview", body, lang, theme, "overview"))

    def _page_host(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        host = api.get_host_snapshot()
        ht = api.get_host_time_overview(self.config_dir, self.state_dir)
        body = f"""
<div class="card" id="live-card"><b>Live network reading</b> (auto-refreshes every 2s, local kernel read only -- no Virtualizor API call):
<div id="live-readout">loading...</div></div>
<div class="card"><b>Server local time:</b> {_html_escape(ht.get('current_local'))}<br>
<b>Uptime:</b> {_html_escape(ht.get('uptime_human'))}<br>
<b>Last boot:</b> {_html_escape(ht.get('boot_time_local'))}<br>
<b>Boot ID:</b> {_html_escape(ht.get('boot_id'))}</div>
<div class="card"><b>CPU:</b> {_html_escape(host.get('cpu'))}</div>
<div class="card"><b>Memory:</b> {_html_escape(host.get('memory'))}</div>
<script>
function fmtBytes(n){{if(n==null)return 'n/a';var u=['B','KB','MB','GB'];var i=0;while(n>=1000&&i<3){{n/=1000;i++;}}return n.toFixed(2)+' '+u[i];}}
function poll(){{fetch('/api/live').then(r=>r.json()).then(d=>{{
  var el=document.getElementById('live-readout');
  if(!d.configured){{el.textContent='No interface configured.';return;}}
  if(!d.ok){{el.textContent='Interface '+d.interface+': unavailable';return;}}
  el.textContent='RX: '+fmtBytes(d.rx_bytes)+'  TX: '+fmtBytes(d.tx_bytes)+' (raw kernel counters, interface '+d.interface+')';
}});}}
poll(); setInterval(poll, 2000);
</script>
"""
        return WebResponse(body=self._render_page("Live Host Monitoring", body, lang, theme, "host"))

    def _page_network(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        net = api.get_network_overview(self.config_dir, self.state_dir)
        if not net.get("configured"):
            body = '<div class="card">No network interface configured yet.</div>'
        else:
            cum = net.get("cumulative") or {}
            rate = net.get("persistent_rate_bytes_per_sec")
            body = f"""
<div class="card"><b>Interface:</b> {_html_escape(net['interface'])}</div>
<div class="card"><b>Persistent Download (RX) Total:</b> {cum.get('cumulative_rx_bytes')}<br>
<b>Persistent Upload (TX) Total:</b> {cum.get('cumulative_tx_bytes')}<br>
<span class="muted">Decimal bytes. Kernel counters are separate and never added to these totals.</span></div>
<div class="card"><b>Persistent sampler rate:</b> {rate if rate else 'unavailable'}</div>
"""
        return WebResponse(body=self._render_page("Network Traffic", body, lang, theme, "network"))

    def _page_traffic(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        net = api.get_network_overview(self.config_dir, self.state_dir)
        if not net.get("configured"):
            body = '<div class="card">No network interface configured yet.</div>'
        else:
            bundle = net.get("traffic_analytics", {})
            rows = ""
            for key in ("today", "yesterday", "current_week", "last_7_days",
                        "current_gregorian_month", "current_jalali_month", "all_time"):
                p = bundle.get(key)
                if not p:
                    continue
                status = p.get("coverage_status", "")
                combined = p.get("combined_bytes")
                rows += (f"<tr><td>{_html_escape(key)}</td><td>{combined}</td>"
                         f"<td>{_html_escape(status)}</td></tr>")
            body = f'<div class="card"><table><tr><th>Period</th><th>Combined bytes</th>' \
                   f'<th>Coverage</th></tr>{rows}</table></div>'
        return WebResponse(body=self._render_page("Traffic Analytics", body, lang, theme, "traffic"))

    def _page_inventory(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        overview = api.get_vps_inventory_overview(self.state_dir)
        records = overview["records"]
        rows = ""
        for r in records[:200]:
            rows += (f'<tr><td><a href="/vps/{r["vpsid"]}">{r["vpsid"]}</a></td>'
                      f'<td>{_html_escape(r.get("hostname"))}</td>'
                      f'<td>{_html_escape(r.get("runtime_status"))}</td>'
                      f'<td>{_html_escape(r.get("quota_gb"))}</td>'
                      f'<td>{_html_escape(r.get("used_gb"))}</td></tr>')
        body = (f'<div class="card">Coverage: {overview["fleet"]["total_vps"]} VPS'
                f'<table><tr><th>ID</th><th>Host</th><th>Status</th><th>Quota</th><th>Used</th></tr>'
                f'{rows or "<tr><td colspan=5>No inventory synced yet.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("VPS Inventory", body, lang, theme, "inventory"))

    def _page_vps_detail(self, user, vpsid_raw, lang, theme) -> WebResponse:
        import control_center_api as api
        try:
            vpsid = int(vpsid_raw)
        except ValueError:
            return self._not_found()
        overview = api.get_vps_inventory_overview(self.state_dir)
        record = next((r for r in overview["records"] if r["vpsid"] == vpsid), None)
        if record is None:
            body = '<div class="card">No such VPS in the current inventory snapshot.</div>'
        else:
            rows = "".join(f"<tr><td>{_html_escape(k)}</td><td>{_html_escape(v)}</td></tr>"
                            for k, v in record.items())
            body = f'<div class="card"><table>{rows}</table></div>'
        return WebResponse(body=self._render_page(f"VPS {vpsid_raw}", body, lang, theme, "inventory"))

    def _page_renewals(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        overview = api.get_renewal_overview(self.config_dir, self.state_dir)
        rows = ""
        for node_id, info in overview.get("per_node", {}).items():
            if not info.get("initialized"):
                rows += f"<tr><td>{_html_escape(node_id)}</td><td colspan=2>not initialized</td></tr>"
                continue
            rows += (f"<tr><td>{_html_escape(node_id)}</td><td>{info['operations_total']}</td>"
                      f"<td>{info['operations_unresolved']}</td></tr>")
        body = f"""
<div class="card"><b>Write approved:</b> {overview.get('write_approved')}
 &nbsp; <b>Auto-renewal:</b> {overview.get('auto_renewal_enabled')}
 &nbsp; <b>Monthly grant:</b> {overview.get('monthly_grant_gb')} GB</div>
<div class="card"><table><tr><th>Node</th><th>Total ops</th><th>Unresolved</th></tr>{rows}</table></div>
"""
        return WebResponse(body=self._render_page("Renewal Operations", body, lang, theme, "renewals"))

    def _page_emergency(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import emergency_policy as emp
        import control_center_api as api
        message = ""
        if method == "POST":
            if not self._require_admin(user):
                return self._forbidden("Admin role required.")
            if not self._check_csrf(token, form.get("csrf_token")):
                return self._forbidden("Invalid CSRF token.")
            new_mode = form.get("mode")
            if new_mode in emp.VALID_MODES:
                settings = emp.load_policy_settings(self.config_dir)
                try:
                    new_settings = emp.ExhaustedBandwidthPolicy(
                        mode=new_mode, emergency_grant_amount_gb=settings.emergency_grant_amount_gb,
                        cooldown_hours=settings.cooldown_hours,
                        max_grants_per_month=settings.max_grants_per_month)
                    emp.save_policy_settings(self.config_dir, new_settings)
                    self._audit(user.username, "emergency_policy_mode_changed", detail=new_mode,
                                remote_addr=remote_addr)
                    message = "<p>Saved.</p>"
                except ValueError as e:
                    message = f"<p>Rejected: {_html_escape(e)}</p>"

        overview = api.get_emergency_policy_overview(self.config_dir, self.state_dir)
        csrf = self._csrf_field(token)
        options = "".join(f'<option value="{m}"{" selected" if m == overview["mode"] else ""}>{m}</option>'
                           for m in emp.VALID_MODES)
        admin_controls = (f'<form method="post">{csrf}<select name="mode">{options}</select>'
                           f'<button type="submit">Save mode</button></form>'
                           if self._require_admin(user) else "")
        body = f"""
<div class="card">{message}<b>Current mode:</b> {_html_escape(overview['mode'])}<br>
Emergency grant amount: {overview['emergency_grant_amount_gb']} GB<br>
Grants-approved marker present: {overview['emergency_grants_approved']}<br>
{admin_controls}</div>
<div class="card"><b>Proposed grants:</b> {len(overview['proposed_grants'])}</div>
"""
        return WebResponse(body=self._render_page("Emergency Bandwidth", body, lang, theme, "emergency"))

    def _page_review(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import emergency_policy as emp
        message = ""
        conn = emp.open_db(os.path.join(self.state_dir, "emergency_policy.sqlite3"))
        try:
            if method == "POST":
                if not self._require_admin(user):
                    return self._forbidden("Admin role required.")
                if not self._check_csrf(token, form.get("csrf_token")):
                    return self._forbidden("Invalid CSRF token.")
                review_id = form.get("review_id")
                action = form.get("action")
                if review_id and action == "resolve":
                    emp.resolve_review(conn, int(review_id), form.get("notes", ""))
                    self._audit(user.username, "manual_review_resolved", detail=review_id,
                                remote_addr=remote_addr)
                    message = "<p>Resolved.</p>"
                elif review_id and action == "acknowledge":
                    emp.acknowledge_review(conn, int(review_id), acknowledged_by=user.username)
                    self._audit(user.username, "manual_review_acknowledged", detail=review_id,
                                remote_addr=remote_addr)
                    message = "<p>Acknowledged.</p>"
            pending = emp.list_pending_reviews(conn)
        finally:
            conn.close()
        csrf = self._csrf_field(token)
        is_admin = self._require_admin(user)
        rows = ""
        for item in pending:
            controls = (f'<form method="post">{csrf}<input type="hidden" name="review_id" value="{item["id"]}">'
                        f'<button name="action" value="acknowledge">Ack</button>'
                        f'<input name="notes" placeholder="notes">'
                        f'<button name="action" value="resolve">Resolve</button></form>'
                        if is_admin else "")
            rows += (f'<tr><td>{item["id"]}</td><td>{item["vpsid"]}</td>'
                      f'<td>{_html_escape(item["reason"])}</td><td>{_html_escape(item["status"])}</td>'
                      f'<td>{controls}</td></tr>')
        body = (f'<div class="card">{message}<table>'
                f'<tr><th>ID</th><th>VPS</th><th>Reason</th><th>Status</th><th></th></tr>'
                f'{rows or "<tr><td colspan=5>No pending reviews.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Manual Review", body, lang, theme, "review"))

    def _page_alerts(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import control_center_api as api
        overview = api.get_emergency_policy_overview(self.config_dir, self.state_dir)
        rows = "".join(f'<tr><td>review</td><td>{r["vpsid"]}</td><td>{_html_escape(r["reason"])}</td></tr>'
                        for r in overview["pending_reviews"])
        rows += "".join(f'<tr><td>grant</td><td>{g["vpsid"]}</td><td>+{g["amount_gb"]} GB proposed</td></tr>'
                         for g in overview["proposed_grants"])
        body = (f'<div class="card"><table><tr><th>Type</th><th>VPS</th><th>Detail</th></tr>'
                f'{rows or "<tr><td colspan=3>No active alerts.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Alerts", body, lang, theme, "alerts"))

    def _page_reports(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import reports as reports_mod
        import telegram_config as tc
        message = ""
        if method == "POST":
            if not self._require_admin(user):
                return self._forbidden("Admin role required.")
            if not self._check_csrf(token, form.get("csrf_token")):
                return self._forbidden("Invalid CSRF token.")
            settings = tc.load_telegram_settings(self.config_dir)
            if not settings.enabled or not settings.admin_ids:
                message = "<p>Telegram bot is not enabled/configured with admins.</p>"
            else:
                try:
                    import telegram_bot as tbot
                    secrets_obj = tc.load_telegram_secrets(tc.default_secrets_path(self.config_dir))
                    transport = tbot.build_transport(settings, secrets_obj)
                    text = reports_mod.generate_report_text(self.config_dir, self.state_dir,
                                                              period_label="Manual (Web)")
                    delivery = reports_mod.send_report_to_admins(transport, settings.admin_ids, text)
                    self._audit(user.username, "report_sent_manually",
                                detail=json.dumps({k: v["ok"] for k, v in delivery.items()}),
                                remote_addr=remote_addr)
                    message = f"<p>Delivery: {delivery}</p>"
                except Exception as e:
                    message = f"<p>Failed: {_html_escape(e)}</p>"
        last_sent = reports_mod.load_last_report_sent_date(self.state_dir)
        settings = tc.load_telegram_settings(self.config_dir)
        csrf = self._csrf_field(token)
        admin_controls = (f'<form method="post">{csrf}<button type="submit">Send report now</button></form>'
                           if self._require_admin(user) else "")
        body = f"""
<div class="card">{message}<b>Scheduled:</b> {settings.reports_enabled}<br>
<b>Schedule:</b> {settings.report_hour:02d}:{settings.report_minute:02d} {_html_escape(settings.report_timezone)}<br>
<b>Last sent:</b> {_html_escape(last_sent or 'never')}<br>{admin_controls}</div>
"""
        return WebResponse(body=self._render_page("Reports", body, lang, theme, "reports"))

    def _page_telegram(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import telegram_config as tc
        settings = tc.load_telegram_settings(self.config_dir)
        secrets_configured = os.path.exists(tc.default_secrets_path(self.config_dir))
        body = f"""
<div class="card"><b>Enabled:</b> {settings.enabled}<br>
<b>Connectivity mode:</b> {_html_escape(settings.connectivity_mode)}<br>
<b>Admins:</b> {len(settings.admin_ids)} &nbsp; <b>Viewers:</b> {len(settings.viewer_ids)}<br>
<b>Secrets configured:</b> {secrets_configured}<br>
<span class="muted">Edit via the CLI menu's "Configure Telegram bot" screen.</span></div>
"""
        return WebResponse(body=self._render_page("Telegram Settings", body, lang, theme, "telegram"))

    def _backup_root(self) -> str:
        return os.path.join(os.path.dirname(self.state_dir), "backups")

    def _page_backups(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import backup as backup_mod
        message = ""
        backup_root = self._backup_root()
        if method == "POST":
            if not self._require_admin(user):
                return self._forbidden("Admin role required.")
            if not self._check_csrf(token, form.get("csrf_token")):
                return self._forbidden("Invalid CSRF token.")
            try:
                archive = backup_mod.create_full_backup_archive(self.state_dir, self.config_dir,
                                                                   backup_root)
                verification = backup_mod.verify_backup_archive(archive)
                self._audit(user.username, "backup_created",
                            detail=f"archive={os.path.basename(archive)} "
                                   f"integrity_ok={verification['ok']}",
                            remote_addr=remote_addr)
                message = (f'<p>Created: {_html_escape(os.path.basename(archive))} '
                           f'(integrity: {"ok" if verification["ok"] else verification})</p>')
            except Exception as e:
                message = f"<p>Backup failed: {_html_escape(str(e))}</p>"

        archives = backup_mod.list_backup_archives(backup_root)
        rows = "".join(f"<tr><td>{_html_escape(a)}</td></tr>" for a in reversed(archives))
        csrf = self._csrf_field(token)
        admin_controls = (f'<form method="post">{csrf}'
                           f'<button type="submit">Create backup now</button></form>'
                           if self._require_admin(user) else "")
        body = f"""
<div class="card">{message}{admin_controls}
<p class="muted">Archives bundle all known state databases plus non-secret config
(never credentials, tokens, or password hashes). Encryption and scheduled/retention
backups are available via the CLI: <code>renewal_cli.py backup-full --encrypt --keep N</code>
(passphrase via the <code>TIXO_BACKUP_PASSPHRASE</code> environment variable, never a CLI
argument). Restoration always extracts to a staging directory for manual review -- it never
overwrites live data automatically.</p>
<table><tr><th>Archive</th></tr>{rows or '<tr><td class="muted">No backups yet.</td></tr>'}</table>
</div>
"""
        return WebResponse(body=self._render_page("Backups", body, lang, theme, "backups"))

    def _page_settings(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import menu as menu_mod
        import host_time as ht_mod
        message = ""
        if method == "POST":
            if not self._require_admin(user):
                return self._forbidden("Admin role required.")
            if not self._check_csrf(token, form.get("csrf_token")):
                return self._forbidden("Invalid CSRF token.")
            tz = form.get("display_tz", "").strip()
            pref = form.get("calendar_preference", "")
            try:
                if tz:
                    menu_mod.save_display_tz(self.config_dir, tz)
                if pref:
                    ht_mod.save_calendar_preference(self.config_dir, pref)
                self._audit(user.username, "settings_changed",
                            detail=f"tz={tz} pref={pref}", remote_addr=remote_addr)
                message = "<p>Saved.</p>"
            except ValueError as e:
                message = f"<p>Rejected: {_html_escape(e)}</p>"

        current_tz = menu_mod.load_display_tz(self.config_dir)
        current_pref = ht_mod.load_calendar_preference(self.config_dir)
        csrf = self._csrf_field(token)
        options = "".join(f'<option value="{p}"{" selected" if p == current_pref else ""}>{p}</option>'
                           for p in ht_mod.VALID_CALENDAR_PREFERENCES)
        admin_form = (f'<form method="post">{csrf}'
                      f'<p>Display timezone: <input name="display_tz" value="{_html_escape(current_tz)}"></p>'
                      f'<p>Calendar preference: <select name="calendar_preference">{options}</select></p>'
                      f'<button type="submit">Save</button></form>' if self._require_admin(user) else "")
        body = f'<div class="card">{message}{admin_form}</div>'
        return WebResponse(body=self._render_page("System Settings", body, lang, theme, "settings"))

    def _page_users(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import web_auth as wa
        message = ""
        if method == "POST":
            if not self._require_admin(user):
                return self._forbidden("Admin role required.")
            if not self._check_csrf(token, form.get("csrf_token")):
                return self._forbidden("Invalid CSRF token.")
            action = form.get("form_action")
            try:
                if action == "create":
                    wa.create_user(self.config_dir, form.get("username", ""),
                                    form.get("password", ""), role=form.get("role", wa.ROLE_VIEWER))
                    self._audit(user.username, "user_created", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = "<p>User created.</p>"
                elif action == "disable":
                    wa.set_user_disabled(self.config_dir, form.get("username", ""), True)
                    self._audit(user.username, "user_disabled", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = "<p>User disabled.</p>"
                elif action == "enable":
                    wa.set_user_disabled(self.config_dir, form.get("username", ""), False)
                    self._audit(user.username, "user_enabled", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = "<p>User enabled.</p>"
                elif action == "delete":
                    wa.delete_user(self.config_dir, form.get("username", ""))
                    self._audit(user.username, "user_deleted", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = "<p>User deleted.</p>"
            except ValueError as e:
                message = f"<p>Rejected: {_html_escape(e)}</p>"

        users = wa.list_users(self.config_dir)
        csrf = self._csrf_field(token)
        is_admin = self._require_admin(user)
        rows = ""
        for u in users:
            controls = ""
            if is_admin:
                toggle_action = "enable" if u.disabled else "disable"
                controls = (f'<form method="post" style="display:inline">{csrf}'
                            f'<input type="hidden" name="username" value="{_html_escape(u.username)}">'
                            f'<button name="form_action" value="{toggle_action}">{toggle_action}</button></form>'
                            f'<form method="post" style="display:inline">{csrf}'
                            f'<input type="hidden" name="username" value="{_html_escape(u.username)}">'
                            f'<button name="form_action" value="delete">delete</button></form>')
            rows += (f'<tr><td>{_html_escape(u.username)}</td><td>{_html_escape(u.role)}</td>'
                      f'<td>{u.disabled}</td><td>{controls}</td></tr>')
        create_form = (f'<form method="post">{csrf}<input type="hidden" name="form_action" value="create">'
                       f'<input name="username" placeholder="username">'
                       f'<input name="password" type="password" placeholder="password">'
                       f'<select name="role"><option value="viewer">viewer</option>'
                       f'<option value="admin">admin</option></select>'
                       f'<button type="submit">Create</button></form>' if is_admin else "")
        body = (f'<div class="card">{message}<table><tr><th>User</th><th>Role</th><th>Disabled</th>'
                f'<th></th></tr>{rows}</table>{create_form}</div>')
        return WebResponse(body=self._render_page("User Management", body, lang, theme, "users"))

    def _page_audit(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import audit_log
        conn = audit_log.open_db(self._audit_db_path())
        try:
            events = audit_log.list_recent_events(conn, limit=100)
        finally:
            conn.close()
        rows = "".join(f'<tr><td>{_html_escape(e["occurred_at"])}</td><td>{_html_escape(e["actor"])}</td>'
                        f'<td>{_html_escape(e["action"])}</td><td>{_html_escape(e["detail"])}</td></tr>'
                        for e in events)
        body = (f'<div class="card"><table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr>'
                f'{rows or "<tr><td colspan=4>No events recorded yet.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Audit Logs", body, lang, theme, "audit"))

    def _page_health(self, user, method, form, token, remote_addr, lang, theme) -> WebResponse:
        import menu as menu_mod
        timers = menu_mod.collect_timer_status()
        rows = ""
        for unit, info in timers.items():
            health = menu_mod.classify_timer_health(info.get("active", ""), info.get("enabled", ""))
            badge = "badge-ok" if health == "ACTIVE" else ("badge-warn" if health == "FAILED" else "badge-off")
            rows += (f'<tr><td>{_html_escape(unit)}</td>'
                      f'<td><span class="badge {badge}">{health}</span></td></tr>')
        body = f'<div class="card"><table><tr><th>Timer</th><th>Health</th></tr>{rows}</table></div>'
        return WebResponse(body=self._render_page("Service Health", body, lang, theme, "health"))


# ---------- socket layer: thin, delegates everything to WebApp.handle_request() ---------------

def _build_handler_class(app: "WebApp"):
    import http.server

    class TixoRequestHandler(http.server.BaseHTTPRequestHandler):
        server_version = "TixoCloudWebServer/1.0"

        def _parse_cookies(self) -> dict:
            raw = self.headers.get("Cookie", "")
            jar = http_cookies.SimpleCookie()
            try:
                jar.load(raw)
            except Exception:
                return {}
            return {k: v.value for k, v in jar.items()}

        def _dispatch(self, method: str):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_REQUEST_BODY_BYTES:
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.rfile.read(length) if length else b""
            cookies = self._parse_cookies()
            remote_addr = self.client_address[0] if self.client_address else "unknown"
            try:
                response = app.handle_request(method, self.path, dict(self.headers), cookies,
                                               body, remote_addr)
            except Exception as e:  # noqa: BLE001 -- a single bad request must never crash the server
                response = WebResponse(status=500, content_type="text/plain; charset=utf-8",
                                        body=f"Internal error: {e}".encode("utf-8"))
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for k, v in response.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            pass  # suppress default stderr request logging -- callers use audit_log instead

    return TixoRequestHandler


class TlsCertificateError(Exception):
    pass


def build_server(config_dir: str, state_dir: str, host: str, port: int,
                  tls_cert_path: str = None, tls_key_path: str = None):
    """Returns a real, bound (but not yet serving) HTTPServer. Caller is responsible for
    calling `.serve_forever()` (blocking) or running it in a thread for tests.

    If BOTH tls_cert_path and tls_key_path are given, the listening socket is wrapped with
    a real TLS server context (manual certificate paths only -- this module never requests
    or provisions a certificate itself; see installer.py's explicit, operator-triggered-only
    automatic-provisioning helper for that). Raises TlsCertificateError (never silently
    falls back to plain HTTP) if the cert/key cannot be loaded."""
    import http.server
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    app = WebApp(config_dir, state_dir, tls_enabled=bool(tls_cert_path and tls_key_path))
    handler_cls = _build_handler_class(app)
    server = ThreadingHTTPServer((host, port), handler_cls)

    if tls_cert_path or tls_key_path:
        if not (tls_cert_path and tls_key_path):
            raise TlsCertificateError(
                "both tls_cert_path and tls_key_path must be set together, never just one")
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=tls_cert_path, keyfile=tls_key_path)
        except (ssl.SSLError, OSError) as e:
            server.server_close()
            raise TlsCertificateError(f"failed to load TLS cert/key: {e}") from None
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    return server


def run_server_in_thread(config_dir: str, state_dir: str, host: str, port: int,
                          tls_cert_path: str = None, tls_key_path: str = None):
    """Starts the server on a background daemon thread and returns (server, thread) -- the
    caller calls `server.shutdown()` to stop it cleanly. Used by tests and by
    renewal_cli.py's `web-run` command alike."""
    server = build_server(config_dir, state_dir, host, port, tls_cert_path, tls_key_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
