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

# Static assets (v1.10.0) -- a fixed allowlist, never a user-supplied filename, so this can
# never be used for path traversal or to expose anything outside this one directory
# (shipped alongside this file: implementation/static/ in dev, lib/static/ once deployed --
# never /root, never anything outside the application's own install tree). Served WITHOUT
# requiring login (the login page itself needs the logo, and browsers request /favicon.ico
# unauthenticated) -- every file here is public-safe branding/script content, nothing
# sensitive.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "/static/tixocloud.png": ("tixocloud.png", "image/png"),
    "/static/tixocloud-dark.png": ("tixocloud-dark.png", "image/png"),
    "/static/live-bandwidth.js": ("live-bandwidth.js", "application/javascript; charset=utf-8"),
    "/favicon.ico": ("tixocloud.png", "image/png"),
}

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

# UI string catalog (v1.9.0, §12 localization) -- every recurring piece of page "chrome"
# (section titles, KPI labels, table column headers, badge/status words, button labels)
# used across all 17 pages, looked up through _t(key, lang) rather than hardcoded inline.
# Long one-off help/explanatory paragraphs are intentionally NOT catalogued here (they are
# lower-value for a quick-glance operational dashboard and would roughly triple this table's
# size) -- those remain English-only, same as this project's existing CLI help text.
UI = {
    "en": {
        "total_vps": "Total VPS", "cpu_load": "CPU load (1m)", "memory": "Memory",
        "host_uptime": "Host uptime", "fleet_host": "Fleet & Host", "network_traffic": "Network Traffic",
        "today": "Today", "yesterday": "Yesterday", "last_7_days": "Last 7 days",
        "this_month_gregorian": "This month (Gregorian)", "this_month_jalali": "This month (Jalali)",
        "this_week": "This week", "all_time": "All time", "last_30_days": "Last 30 days",
        "network_interface": "Network interface", "configured_on": "configured on",
        "not_configured": "not configured", "renewal": "Renewal", "write_approved": "write-approved",
        "auto_renewal": "auto-renewal", "emergency_policy_mode": "Emergency policy mode",
        "write_approved_label": "Write approved", "auto_renewal_label": "Auto-renewal",
        "monthly_grant_label": "Monthly grant", "grant_amount_label": "Grant amount",
        "active_alerts": "Active alerts", "daily_history": "Daily history", "standard_periods": "Standard periods",
        "last_7d_days": "Last 7 Days", "view_full_history": "view full history",
        "download": "Download", "upload": "Upload", "combined": "Combined", "coverage": "Coverage",
        "period": "Period", "full_coverage": "full coverage", "partial": "partial", "no_data": "no data",
        "cpu": "CPU", "model": "Model", "cores_threads": "Cores / Threads", "load_average": "Load average",
        "ram_used": "RAM used", "swap_used": "Swap used", "uptime": "Uptime", "local_time": "Local time",
        "last_boot": "Last boot", "host": "Host", "technical_details": "Technical details",
        "interface": "Interface", "persistent_total": "Persistent Total (since monitoring started)",
        "current_sampler_rate": "Current sampler rate", "combined_rate": "Combined rate",
        "vps_id": "VPS ID", "name": "Name", "ip": "IP", "status": "Status", "used": "Used",
        "limit": "Limit", "usage_pct": "Usage %", "running": "running", "stopped": "stopped",
        "suspended": "suspended", "unlimited": "unlimited", "unavailable": "unavailable",
        "node": "Node", "total_ops": "Total ops", "unresolved": "Unresolved",
        "per_node_operations": "Per-node operations", "monthly_grant": "Monthly grant",
        "current_mode": "Current mode", "grant_amount": "Grant amount",
        "grants_approved_marker": "Grants-approved marker", "proposed_grants": "Proposed grants",
        "id": "ID", "vps": "VPS", "reason": "Reason", "detail": "Detail", "type": "Type",
        "enabled": "Enabled", "connectivity_mode": "Connectivity mode", "admins_viewers": "Admins / Viewers",
        "secrets_configured": "Secrets configured", "scheduled": "Scheduled", "schedule": "Schedule",
        "last_sent": "Last sent", "archives_on_disk": "Archives on disk", "archive": "Archive",
        "display_timezone": "Display timezone", "calendar_preference": "Calendar preference",
        "user": "User", "role": "Role", "disabled": "Disabled", "time": "Time", "actor": "Actor",
        "action": "Action", "timers_tracked": "Timers tracked", "active": "Active", "failed": "Failed",
        "timer": "Timer", "health": "Health", "save": "Save", "save_mode": "Save mode",
        "create": "Create", "send_report_now": "Send report now", "create_backup_now": "Create backup now",
        "saved": "Saved.", "fleet": "Fleet",
        "live_bandwidth": "Live Network Bandwidth", "live_history": "Persistent Totals",
        "pause": "Pause", "resume": "Resume", "live": "Live", "stale": "Stale",
        "collecting": "Collecting live data...", "unit": "Unit", "window": "Window",
        "last_1m": "1 min", "last_5m": "5 min", "last_15m": "15 min", "auto": "Auto",
    },
    "fa": {
        "total_vps": "تعداد کل VPS", "cpu_load": "بار CPU (۱ دقیقه)", "memory": "حافظه",
        "host_uptime": "مدت کار میزبان", "fleet_host": "ناوگان و میزبان", "network_traffic": "ترافیک شبکه",
        "today": "امروز", "yesterday": "دیروز", "last_7_days": "۷ روز گذشته",
        "this_month_gregorian": "این ماه (میلادی)", "this_month_jalali": "این ماه (شمسی)",
        "this_week": "این هفته", "all_time": "تمام مدت", "last_30_days": "۳۰ روز گذشته",
        "network_interface": "واسط شبکه", "configured_on": "پیکربندی‌شده روی",
        "not_configured": "پیکربندی نشده", "renewal": "تمدید", "write_approved": "نوشتن تأییدشده",
        "auto_renewal": "تمدید خودکار", "emergency_policy_mode": "حالت سیاست اضطراری",
        "write_approved_label": "نوشتن تأییدشده", "auto_renewal_label": "تمدید خودکار",
        "monthly_grant_label": "اعطای ماهانه", "grant_amount_label": "مقدار اعطا",
        "active_alerts": "هشدارهای فعال", "daily_history": "تاریخچه روزانه", "standard_periods": "دوره‌های استاندارد",
        "last_7d_days": "۷ روز گذشته", "view_full_history": "مشاهده تاریخچه کامل",
        "download": "دریافت", "upload": "ارسال", "combined": "مجموع", "coverage": "پوشش",
        "period": "دوره", "full_coverage": "پوشش کامل", "partial": "ناقص", "no_data": "بدون داده",
        "cpu": "پردازنده", "model": "مدل", "cores_threads": "هسته‌ها / رشته‌ها", "load_average": "میانگین بار",
        "ram_used": "رم استفاده‌شده", "swap_used": "سواپ استفاده‌شده", "uptime": "مدت کار", "local_time": "زمان محلی",
        "last_boot": "آخرین بوت", "host": "میزبان", "technical_details": "جزئیات فنی",
        "interface": "واسط", "persistent_total": "مجموع پایدار (از زمان شروع نظارت)",
        "current_sampler_rate": "نرخ نمونه‌گیری فعلی", "combined_rate": "نرخ مجموع",
        "vps_id": "شناسه VPS", "name": "نام", "ip": "آی‌پی", "status": "وضعیت", "used": "استفاده‌شده",
        "limit": "سقف", "usage_pct": "درصد استفاده", "running": "در حال اجرا", "stopped": "متوقف",
        "suspended": "معلق", "unlimited": "نامحدود", "unavailable": "در دسترس نیست",
        "node": "گره", "total_ops": "کل عملیات", "unresolved": "حل‌نشده",
        "per_node_operations": "عملیات هر گره", "monthly_grant": "اعطای ماهانه",
        "current_mode": "حالت فعلی", "grant_amount": "مقدار اعطا",
        "grants_approved_marker": "نشانگر تأیید اعطا", "proposed_grants": "اعطاهای پیشنهادی",
        "id": "شناسه", "vps": "VPS", "reason": "دلیل", "detail": "جزئیات", "type": "نوع",
        "enabled": "فعال", "connectivity_mode": "حالت اتصال", "admins_viewers": "مدیران / بینندگان",
        "secrets_configured": "اطلاعات محرمانه پیکربندی‌شده", "scheduled": "زمان‌بندی‌شده", "schedule": "زمان‌بندی",
        "last_sent": "آخرین ارسال", "archives_on_disk": "آرشیوهای موجود", "archive": "آرشیو",
        "display_timezone": "منطقه زمانی نمایش", "calendar_preference": "ترجیح تقویم",
        "user": "کاربر", "role": "نقش", "disabled": "غیرفعال", "time": "زمان", "actor": "عامل",
        "action": "عملیات", "timers_tracked": "تایمرهای پایش‌شده", "active": "فعال", "failed": "ناموفق",
        "timer": "تایمر", "health": "سلامت", "save": "ذخیره", "save_mode": "ذخیره حالت",
        "create": "ایجاد", "send_report_now": "ارسال گزارش اکنون", "create_backup_now": "ایجاد پشتیبان اکنون",
        "saved": "ذخیره شد.", "fleet": "ناوگان",
        "live_bandwidth": "پهنای باند زنده شبکه", "live_history": "مجموع‌های پایدار",
        "pause": "توقف", "resume": "ادامه", "live": "زنده", "stale": "قدیمی",
        "collecting": "در حال جمع‌آوری داده زنده...", "unit": "واحد", "window": "بازه زمانی",
        "last_1m": "۱ دقیقه", "last_5m": "۵ دقیقه", "last_15m": "۱۵ دقیقه", "auto": "خودکار",
    },
}


def _t(key: str, lang: str) -> str:
    """Looks up `key` in the UI string catalog for `lang`, falling back to the English
    string (never a raw key or an empty string) if `lang` is invalid or the key is missing
    -- a missing translation must never surface as a blank label."""
    table = UI.get(lang) or UI["en"]
    return table.get(key, UI["en"].get(key, key))


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

# v1.9.0 design system -- a genuine visual overhaul (modern SaaS-panel look: refined
# palette, elevation/shadows, consistent spacing scale, KPI/metric cards, progress gauges,
# sticky-header dense tables, smooth transitions) while remaining PURE CSS: zero external
# fonts, zero icon packs, zero CDN, zero added CPU/RAM/network cost on the hypervisor. The
# only visual "icons" are inline Unicode glyphs already in any system font.
CSS = """
:root[data-theme="dark"] {
  --bg:#0b0d12; --bg-elevated:#12151c; --card:#161a22; --card-hover:#1b2029;
  --accent:#ef4444; --accent-soft:#ef44441a; --accent-strong:#dc2626;
  --border:#232834; --muted:#8b93a1; --fg:#e8eaed;
  --ok:#22c55e; --ok-soft:#22c55e1a; --warn:#f59e0b; --warn-soft:#f59e0b1a;
  --bad:#ef4444; --bad-soft:#ef44441a; --info:#3b82f6; --info-soft:#3b82f61a;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.25);
}
:root[data-theme="light"] {
  --bg:#f4f5f7; --bg-elevated:#ffffff; --card:#ffffff; --card-hover:#f8f9fb;
  --accent:#dc2626; --accent-soft:#dc26261a; --accent-strong:#b91c1c;
  --border:#e3e6ea; --muted:#667085; --fg:#111418;
  --ok:#16a34a; --ok-soft:#16a34a1a; --warn:#d97706; --warn-soft:#d977061a;
  --bad:#dc2626; --bad-soft:#dc26261a; --info:#2563eb; --info-soft:#2563eb1a;
  --shadow: 0 1px 2px rgba(16,24,40,.06), 0 4px 16px rgba(16,24,40,.06);
}
* { box-sizing: border-box; }
html, body { margin:0; }
body { background:var(--bg); color:var(--fg); margin:0; display:flex; min-height:100vh;
  font-family: "Segoe UI", -apple-system, "Vazirmatn", Tahoma, Roboto, sans-serif;
  font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased; }
[dir="rtl"] body { direction: rtl; }
a { color: inherit; }

/* ---------- sidebar ---------- */
nav { width:236px; background:var(--bg-elevated); border-right:1px solid var(--border);
  padding:20px 14px; flex-shrink:0; position:sticky; top:0; height:100vh; overflow-y:auto; }
[dir="rtl"] nav { border-right:none; border-left:1px solid var(--border); }
.brand-logo { display:block; height:34px; width:auto; max-width:100%; object-fit:contain;
  margin:4px 10px 18px; }
nav a { display:flex; align-items:center; gap:10px; color:var(--muted); text-decoration:none;
  padding:9px 12px; border-radius:8px; margin-bottom:2px; font-size:13.5px; font-weight:500;
  transition: background .12s ease, color .12s ease; }
nav a:hover { background:var(--card-hover); color:var(--fg); }
nav a.active { background:var(--accent-soft); color:var(--accent); font-weight:600; }
nav form { margin:16px 4px 0; padding-top:14px; border-top:1px solid var(--border); }
nav button { width:100%; }

/* ---------- main content ---------- */
main { flex:1; padding:28px 32px; max-width:1180px; width:100%; margin:0 auto; }
main > h1 { font-size:22px; font-weight:700; margin:0 0 20px; letter-spacing:-.2px; }
footer.muted { padding:20px 32px; font-size:11.5px; }

/* ---------- cards ---------- */
.card { background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:18px 20px; margin-bottom:18px; box-shadow:var(--shadow); }
.card h2, .card h3 { margin-top:0; font-size:14px; font-weight:600; color:var(--muted);
  text-transform:uppercase; letter-spacing:.4px; }
.card-row { display:flex; flex-wrap:wrap; gap:16px; margin-bottom:18px; }
.card-row .card { flex:1 1 220px; margin-bottom:0; }

/* ---------- KPI / metric cards ---------- */
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr));
  gap:14px; margin-bottom:18px; }
.kpi { background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:16px 18px; box-shadow:var(--shadow); }
.kpi .kpi-label { color:var(--muted); font-size:12px; font-weight:600; text-transform:uppercase;
  letter-spacing:.4px; margin-bottom:6px; }
.kpi .kpi-value { font-size:24px; font-weight:700; letter-spacing:-.3px; }
.kpi .kpi-sub { color:var(--muted); font-size:12px; margin-top:4px; }
.kpi.kpi-ok .kpi-value { color:var(--ok); } .kpi.kpi-warn .kpi-value { color:var(--warn); }
.kpi.kpi-bad .kpi-value { color:var(--bad); } .kpi.kpi-info .kpi-value { color:var(--info); }

/* ---------- gauges / progress ---------- */
.gauge { height:8px; border-radius:6px; background:var(--border); overflow:hidden; margin-top:8px; }
.gauge > span { display:block; height:100%; border-radius:6px; transition:width .3s ease; }
.gauge.g-ok > span { background:var(--ok); } .gauge.g-warn > span { background:var(--warn); }
.gauge.g-bad > span { background:var(--bad); }
.gauge-label { display:flex; justify-content:space-between; font-size:12px; color:var(--muted); margin-top:4px; }

/* ---------- tables ---------- */
table { width:100%; border-collapse:collapse; font-size:13px; }
thead th { position:sticky; top:0; background:var(--card); text-align:left; padding:10px 10px;
  border-bottom:2px solid var(--border); color:var(--muted); font-weight:600; font-size:11.5px;
  text-transform:uppercase; letter-spacing:.3px; }
tbody td { padding:9px 10px; border-bottom:1px solid var(--border); }
tbody tr:hover { background:var(--card-hover); }
[dir="rtl"] th, [dir="rtl"] td { text-align:right; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
[dir="rtl"] .num { text-align:left; }
.table-wrap { overflow-x:auto; }

/* ---------- misc ---------- */
.muted { color:var(--muted); font-size:12.5px; }
.badge { display:inline-flex; align-items:center; gap:4px; padding:3px 10px; border-radius:20px;
  font-size:11.5px; font-weight:600; letter-spacing:.2px; }
.badge-ok { background:var(--ok-soft); color:var(--ok); }
.badge-warn { background:var(--warn-soft); color:var(--warn); }
.badge-bad { background:var(--bad-soft); color:var(--bad); }
.badge-off { background:var(--border); color:var(--muted); }
.badge-info { background:var(--info-soft); color:var(--info); }
input, select, button { padding:8px 12px; border-radius:8px; border:1px solid var(--border);
  background:var(--bg-elevated); color:var(--fg); font-size:13px; font-family:inherit; }
button { background:var(--accent); color:#fff; cursor:pointer; border:none; font-weight:600;
  transition:background .12s ease, opacity .12s ease; }
button:hover { background:var(--accent-strong); }
button:active { opacity:.85; }
button.secondary { background:var(--card-hover); color:var(--fg); border:1px solid var(--border); }
button.secondary:hover { background:var(--border); }
form { margin-bottom:8px; }
.topbar { display:flex; justify-content:flex-end; align-items:center; gap:10px; padding:12px 32px;
  border-bottom:1px solid var(--border); background:var(--bg-elevated); }
.section-title { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.5px;
  color:var(--muted); margin:22px 0 10px; }
.chart-wrap { width:100%; overflow-x:auto; }
.chart-wrap svg { max-width:100%; }
.empty-state { color:var(--muted); font-size:13px; padding:20px; text-align:center; }
.freshness { font-size:11.5px; padding:2px 8px; border-radius:6px; }

/* ---------- login ---------- */
.login-card { max-width:360px; margin:10vh auto 0; text-align:center; }
.login-logo { display:block; height:48px; width:auto; max-width:100%; object-fit:contain;
  margin:0 auto 22px; }
.login-card input { width:100%; }
.login-card button { width:100%; }

/* ---------- live bandwidth widget ---------- */
.live-bw-header { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between;
  gap:10px; margin-bottom:12px; }
.live-bw-controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.live-bw-controls select, .live-bw-controls button { font-size:12.5px; padding:6px 10px; }
.live-bw-iface { font-size:12px; }
.live-bw-status { margin-top:8px; }
.live-bw-ok { color:var(--ok); }
.live-bw-warn { color:var(--warn); }
.live-bw-bad { color:var(--bad); }
.live-bw-svg { width:100%; height:auto; }

@media (max-width: 900px) {
  body { flex-direction:column; }
  nav { width:100%; height:auto; position:static; padding:12px; }
  nav a { padding:10px 12px; }
  main { padding:18px; max-width:100%; }
  .kpi-grid { grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); }
}
@media (max-width: 560px) {
  .kpi-grid { grid-template-columns:1fr 1fr; }
  table { font-size:12px; }
}
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
        # A page whose title is exactly its nav entry's English label (every standard page)
        # gets the localized nav label as its on-page heading too -- so a Persian-language
        # session never sees an English heading on top of an otherwise-translated page. A
        # page with a dynamic title (e.g. "VPS 42") keeps that exact title untouched, since
        # it has no corresponding catalog entry.
        heading = labels[active_nav] if active_nav and title == LABELS["en"].get(active_nav) else title
        logo_file = "/static/tixocloud.png" if theme == "dark" else "/static/tixocloud-dark.png"
        return f"""<!DOCTYPE html>
<html lang="{lang}" dir="{direction}" data-theme="{theme}">
<head><meta charset="utf-8"><title>TixoCloud -- {_html_escape(title)}</title>
<link rel="icon" type="image/png" href="/favicon.ico">
<style>{CSS}</style></head>
<body>
<nav><img class="brand-logo" src="{logo_file}" alt="TixoCloud">{nav_html}
<form method="post" action="/logout"><button type="submit">{labels['logout']}</button></form>
</nav>
<main><h1>{_html_escape(heading)}</h1>{body_html}</main>
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

        if route in STATIC_FILES and method == "GET":
            return self._handle_static(route)

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
            return handler(user, method, form, token, remote_addr, lang, theme, query)

        return self._not_found()

    # ---------- auth -----------------------------------------------------------------------

    def _handle_login(self, method, form, cookies, remote_addr, lang, theme) -> WebResponse:
        import web_auth as wa
        import security as sec

        logo_file = "/static/tixocloud.png" if theme == "dark" else "/static/tixocloud-dark.png"
        if method == "GET":
            body = f"""
<div class="card login-card">
<img class="login-logo" src="{logo_file}" alt="TixoCloud">
<form method="post" action="/login">
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
            body = (f'<div class="card login-card"><img class="login-logo" src="{logo_file}" alt="TixoCloud">'
                    '<p class="badge badge-bad">Invalid credentials.</p>'
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

    # ---------- static assets ----------------------------------------------------------------

    def _handle_static(self, route: str) -> WebResponse:
        filename, content_type = STATIC_FILES[route]
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._not_found()
        return WebResponse(status=200, content_type=content_type, body=data,
                            headers={"Cache-Control": "public, max-age=3600"})

    # ---------- live JSON API ---------------------------------------------------------------

    def _handle_api_live(self, query) -> WebResponse:
        import control_center_api as api
        import hostnet_sync
        import traffic_format as tf
        settings = hostnet_sync.load_settings(self.config_dir)
        interface = settings.get("interface")
        if not interface:
            return self._json({"configured": False})
        live = api.get_live_network_reading(interface)
        block = tf.format_traffic_block(live.get("rx_bytes"), live.get("tx_bytes")) if live.get("ok") else {
            "download": "unavailable", "upload": "unavailable", "combined": "unavailable"}
        return self._json({"configured": True, "interface": interface, **live, **block})

    # ---------- pages (each: user, method, form, session_token, remote_addr, lang, theme) ---

    def _render_live_bandwidth_widget(self, lang: str, widget_id: str) -> str:
        """The LIVE bandwidth section (v1.10.0) embedded on Overview and Network Traffic --
        markup + i18n labels only; all polling/rate-computation/charting happens client-side
        in static/live-bandwidth.js against the existing, already-authenticated /api/live
        endpoint (one cheap /sys/class/net read per poll -- no new backend process, no extra
        load). See that file's docstring for the full honesty/bounded-buffer discipline."""
        return f"""
<div class="card" id="{widget_id}"
     data-l-download="{_t('download', lang)}" data-l-upload="{_t('upload', lang)}"
     data-l-combined="{_t('combined', lang)}" data-l-pause="{_t('pause', lang)}"
     data-l-resume="{_t('resume', lang)}" data-l-live="{_t('live', lang)}"
     data-l-stale="{_t('stale', lang)}" data-l-unavailable="{_t('unavailable', lang)}"
     data-l-collecting="{_t('collecting', lang)}" data-l-interface="{_t('interface', lang)}">
  <div class="live-bw-header">
    <div><b>{_t('live_bandwidth', lang)}</b> <span class="muted live-bw-iface"></span></div>
    <div class="live-bw-controls">
      <select class="live-bw-unit" aria-label="{_t('unit', lang)}">
        <option value="auto">{_t('auto', lang)}</option>
        <option value="mbps">Mbps</option>
        <option value="gbps">Gbps</option>
        <option value="MBps">MB/s</option>
        <option value="GBps">GB/s</option>
      </select>
      <select class="live-bw-window" aria-label="{_t('window', lang)}">
        <option value="60">{_t('last_1m', lang)}</option>
        <option value="300" selected>{_t('last_5m', lang)}</option>
        <option value="900">{_t('last_15m', lang)}</option>
      </select>
      <button type="button" class="secondary live-bw-pause">{_t('pause', lang)}</button>
    </div>
  </div>
  <div class="kpi-grid live-bw-values"></div>
  <div class="chart-wrap"><svg class="live-bw-svg" viewBox="0 0 760 200"></svg></div>
  <div class="live-bw-status muted">{_t('collecting', lang)}</div>
  <div class="section-title">{_t('live_history', lang)}</div>
  <div class="kpi-grid live-bw-cumulative"></div>
</div>
<script src="/static/live-bandwidth.js"></script>
<script>TixoLiveBandwidth.mount("{widget_id}");</script>
"""

    def _period_kpi(self, bundle: dict, key: str, label_key: str, lang: str) -> str:
        """Renders one traffic-analytics period as a KPI card -- download/upload/combined via
        the shared `traffic_format` layer, with an honest coverage badge (never implying full
        coverage where samples are missing or partial). `label_key` is a UI catalog key."""
        import traffic_format as tf
        label = _t(label_key, lang)
        p = bundle.get(key)
        if not p:
            return (f'<div class="kpi"><div class="kpi-label">{_html_escape(label)}</div>'
                    f'<div class="kpi-value muted">{_t("unavailable", lang)}</div></div>')
        block = tf.format_traffic_block(p.get("rx_bytes"), p.get("tx_bytes"))
        status = p.get("coverage_status", "")
        badge_class = {"FULL": "badge-ok", "PARTIAL_COVERAGE": "badge-warn"}.get(status, "badge-off")
        badge_text = {"FULL": _t("full_coverage", lang), "PARTIAL_COVERAGE": _t("partial", lang),
                      "NO_DATA": _t("no_data", lang)}.get(status, status.lower() if status else "n/a")
        return (f'<div class="kpi"><div class="kpi-label">{_html_escape(label)}</div>'
                f'<div class="kpi-value">{block["combined"]}</div>'
                f'<div class="kpi-sub">&darr; {block["download"]} &nbsp; &uarr; {block["upload"]}</div>'
                f'<div class="kpi-sub"><span class="badge {badge_class}">{badge_text}</span></div></div>')

    def _page_overview(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        import traffic_format as tf
        snap = api.get_full_snapshot(self.config_dir, self.state_dir)
        fleet = snap["inventory"]["fleet"]
        net = snap["network"]
        ht = snap["host_time"]
        host = snap.get("host") or {}
        renewal = snap["renewal"]
        emp = snap["emergency_policy"]
        cpu = host.get("cpu") or {}
        usage = host.get("usage") or {}
        load = (host.get("uptime") or {}).get("load_average") or {}

        running = fleet.get("running_count", 0)
        total_vps = fleet.get("total_vps", 0)
        stopped = fleet.get("stopped_count", 0)
        suspended = fleet.get("suspended_count", 0)
        unknown = fleet.get("unknown_status_count", 0)

        mem_pct = usage.get("memory_percent")
        mem_pct = mem_pct if isinstance(mem_pct, (int, float)) else None
        load1 = load.get("1m")
        logical_cpus = cpu.get("logical_cpus")
        load_pct = (round(100 * load1 / logical_cpus, 1)
                    if isinstance(load1, (int, float)) and isinstance(logical_cpus, int) and logical_cpus > 0
                    else None)

        def _gauge_class(pct):
            if pct is None:
                return "g-ok"
            return "g-bad" if pct >= 90 else "g-warn" if pct >= 75 else "g-ok"

        kpi_fleet = f"""
<div class="kpi kpi-info"><div class="kpi-label">{_t('total_vps', lang)}</div><div class="kpi-value">{total_vps}</div>
<div class="kpi-sub">{running} {_t('running', lang)} &nbsp; {stopped} {_t('stopped', lang)} &nbsp; {suspended} {_t('suspended', lang)} &nbsp; {unknown} ?</div></div>
<div class="kpi"><div class="kpi-label">{_t('cpu_load', lang)}</div>
<div class="kpi-value">{load1 if load1 is not None else 'n/a'}</div>
<div class="kpi-sub">{logical_cpus if logical_cpus else '?'} logical CPUs</div>
<div class="gauge {_gauge_class(load_pct)}"><span style="width:{min(load_pct, 100) if load_pct is not None else 0}%"></span></div></div>
<div class="kpi"><div class="kpi-label">{_t('memory', lang)}</div>
<div class="kpi-value">{mem_pct if mem_pct is not None else 'n/a'}{'%' if mem_pct is not None else ''}</div>
<div class="gauge {_gauge_class(mem_pct)}"><span style="width:{min(mem_pct, 100) if mem_pct is not None else 0}%"></span></div></div>
<div class="kpi"><div class="kpi-label">{_t('host_uptime', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(ht.get('uptime_human'))}</div>
<div class="kpi-sub">{_html_escape(ht.get('current_local'))}</div></div>
"""
        bundle = net.get("traffic_analytics", {}) if net.get("configured") else {}
        traffic_kpis = "".join([
            self._period_kpi(bundle, "today", "today", lang),
            self._period_kpi(bundle, "yesterday", "yesterday", lang),
            self._period_kpi(bundle, "last_7_days", "last_7_days", lang),
            self._period_kpi(bundle, "current_gregorian_month", "this_month_gregorian", lang),
            self._period_kpi(bundle, "current_jalali_month", "this_month_jalali", lang),
        ]) if bundle else '<div class="empty-state">No network interface configured yet -- traffic figures unavailable.</div>'

        n_alerts = len(emp.get("pending_reviews", [])) + len(emp.get("proposed_grants", []))

        chart_block = ""
        if net.get("configured"):
            import charts
            series = net.get("daily_series_7d", [])
            chart_svg = charts.render_daily_bar_chart(series, width=760, height=190,
                                                        title="Last 7 days download/upload")
            chart_block = (f'<div class="section-title">{_t("last_7d_days", lang)} &nbsp; '
                           f'<a href="/traffic" class="muted" style="text-decoration:none">{_t("view_full_history", lang)} &rarr;</a></div>'
                           f'<div class="card chart-wrap">{chart_svg}</div>')

        live_widget = self._render_live_bandwidth_widget(lang, "live-bw-overview") if net.get("configured") else ""

        body = f"""
<div class="section-title">{_t('fleet_host', lang)}</div>
<div class="kpi-grid">{kpi_fleet}</div>
{live_widget}
<div class="section-title">{_t('network_traffic', lang)}</div>
<div class="kpi-grid">{traffic_kpis}</div>
{chart_block}
<div class="card-row">
<div class="card"><b>{_t('network_interface', lang)}:</b> {'<span class="badge badge-ok">' + _t('configured_on', lang) + ' ' + _html_escape(net['interface']) + '</span>' if net.get('configured') else '<span class="badge badge-off">' + _t('not_configured', lang) + '</span>'}</div>
<div class="card"><b>{_t('renewal', lang)}:</b> {_t('write_approved', lang)}
<span class="badge {'badge-ok' if renewal.get('write_approved') else 'badge-off'}">{renewal.get('write_approved')}</span>
&nbsp; {_t('auto_renewal', lang)}
<span class="badge {'badge-ok' if renewal.get('auto_renewal_enabled') else 'badge-off'}">{renewal.get('auto_renewal_enabled')}</span></div>
<div class="card"><b>{_t('emergency_policy_mode', lang)}:</b> {_html_escape(emp['mode'])}
&nbsp; <b>{_t('active_alerts', lang)}:</b> <span class="badge {'badge-warn' if n_alerts else 'badge-ok'}">{n_alerts}</span></div>
</div>
"""
        return WebResponse(body=self._render_page("Overview", body, lang, theme, "overview"))

    def _page_host(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        host = api.get_host_snapshot()
        ht = api.get_host_time_overview(self.config_dir, self.state_dir)
        cpu = host.get("cpu") or {}
        mem = host.get("memory") or {}
        usage = host.get("usage") or {}
        uptime = host.get("uptime") or {}
        load = uptime.get("load_average") or {}
        mem_total = mem.get("total_mb")
        mem_avail = mem.get("available_mb")
        mem_used = (mem_total - mem_avail) if isinstance(mem_total, int) and isinstance(mem_avail, int) else None
        mem_pct = usage.get("memory_percent")
        mem_pct = mem_pct if isinstance(mem_pct, (int, float)) else None
        swap_total = mem.get("swap_total_mb")
        swap_free = mem.get("swap_free_mb")
        swap_used = (swap_total - swap_free) if isinstance(swap_total, int) and isinstance(swap_free, int) else None

        def _gauge_class(pct):
            if pct is None:
                return "g-ok"
            return "g-bad" if pct >= 90 else "g-warn" if pct >= 75 else "g-ok"

        body = f"""
<div class="card" id="live-card"><b>Live network reading</b> <span class="muted">(auto-refreshes every 2s, local kernel read only -- no Virtualizor API call)</span>
<div id="live-readout" class="kpi-value" style="margin-top:8px">loading...</div></div>
<div class="section-title">{_t('cpu', lang)}</div>
<div class="kpi-grid">
<div class="kpi"><div class="kpi-label">{_t('model', lang)}</div><div class="kpi-value" style="font-size:15px">{_html_escape(cpu.get('model'))}</div>
<div class="kpi-sub">{_html_escape(cpu.get('vendor'))}</div></div>
<div class="kpi"><div class="kpi-label">{_t('cores_threads', lang)}</div>
<div class="kpi-value">{_html_escape(cpu.get('physical_cores'))} / {_html_escape(cpu.get('logical_cpus'))}</div>
<div class="kpi-sub">physical cores / logical CPUs</div></div>
<div class="kpi"><div class="kpi-label">{_t('load_average', lang)}</div>
<div class="kpi-value">{_html_escape(load.get('1m'))}</div>
<div class="kpi-sub">1m / 5m / 15m: {_html_escape(load.get('1m'))} / {_html_escape(load.get('5m'))} / {_html_escape(load.get('15m'))}</div></div>
</div>
<div class="section-title">{_t('memory', lang)}</div>
<div class="kpi-grid">
<div class="kpi"><div class="kpi-label">{_t('ram_used', lang)}</div>
<div class="kpi-value">{mem_pct if mem_pct is not None else 'n/a'}{'%' if mem_pct is not None else ''}</div>
<div class="kpi-sub">{mem_used if mem_used is not None else '?'} MB of {_html_escape(mem_total)} MB</div>
<div class="gauge {_gauge_class(mem_pct)}"><span style="width:{min(mem_pct, 100) if mem_pct is not None else 0}%"></span></div></div>
<div class="kpi"><div class="kpi-label">{_t('swap_used', lang)}</div>
<div class="kpi-value">{swap_used if swap_used is not None else 'n/a'} MB</div>
<div class="kpi-sub">of {_html_escape(swap_total)} MB total</div></div>
</div>
<div class="section-title">{_t('host', lang)}</div>
<div class="kpi-grid">
<div class="kpi"><div class="kpi-label">{_t('uptime', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(ht.get('uptime_human'))}</div></div>
<div class="kpi"><div class="kpi-label">{_t('local_time', lang)}</div><div class="kpi-value" style="font-size:15px">{_html_escape(ht.get('current_local'))}</div></div>
<div class="kpi"><div class="kpi-label">{_t('last_boot', lang)}</div><div class="kpi-value" style="font-size:15px">{_html_escape(ht.get('boot_time_local'))}</div></div>
</div>
<div class="section-title">{_t('technical_details', lang)}</div>
<div class="card muted">Boot ID: {_html_escape(ht.get('boot_id'))} &nbsp; Kernel: {_html_escape(host.get('os', {}).get('kernel'))}
 &nbsp; Arch: {_html_escape(host.get('os', {}).get('architecture'))}</div>
<script>
function poll(){{fetch('/api/live').then(r=>r.json()).then(d=>{{
  var el=document.getElementById('live-readout');
  if(!d.configured){{el.textContent='No interface configured.';return;}}
  if(!d.ok){{el.textContent='Interface '+d.interface+': unavailable';return;}}
  el.textContent='Download: '+d.download+'  Upload: '+d.upload+'  Combined: '+d.combined+' (interface '+d.interface+')';
}});}}
poll(); setInterval(poll, 2000);
</script>
"""
        return WebResponse(body=self._render_page("Live Host Monitoring", body, lang, theme, "host"))

    def _page_network(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        import traffic_format as tf
        net = api.get_network_overview(self.config_dir, self.state_dir)
        if not net.get("configured"):
            body = '<div class="empty-state">No network interface configured yet.</div>'
        else:
            cum = net.get("cumulative") or {}
            rate = net.get("persistent_rate_bytes_per_sec")
            block = tf.format_traffic_block(cum.get("cumulative_rx_bytes"), cum.get("cumulative_tx_bytes"))
            rate_str = tf.format_bits_per_second(rate) if isinstance(rate, (int, float)) else None
            live_widget = self._render_live_bandwidth_widget(lang, "live-bw-network")
            body = f"""
<div class="card"><b>{_t('interface', lang)}:</b> {_html_escape(net['interface'])}</div>
{live_widget}
<div class="section-title">{_t('persistent_total', lang)}</div>
<div class="kpi-grid">
<div class="kpi"><div class="kpi-label">{_t('download', lang)} (RX)</div><div class="kpi-value">{block['download']}</div></div>
<div class="kpi"><div class="kpi-label">{_t('upload', lang)} (TX)</div><div class="kpi-value">{block['upload']}</div></div>
<div class="kpi kpi-info"><div class="kpi-label">{_t('combined', lang)}</div><div class="kpi-value">{block['combined']}</div></div>
</div>
<div class="card muted">Decimal (SI) bytes. Raw kernel counters are tracked separately and never added into
this persistent total (that would double-count across counter resets/reboots).</div>
"""
            if rate_str:
                body += f"""
<div class="section-title">{_t('current_sampler_rate', lang)}</div>
<div class="kpi-grid"><div class="kpi"><div class="kpi-label">{_t('combined_rate', lang)}</div><div class="kpi-value">{rate_str}</div></div></div>
"""
        return WebResponse(body=self._render_page("Network Traffic", body, lang, theme, "network"))

    def _page_traffic(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        import traffic_format as tf
        import charts
        net = api.get_network_overview(self.config_dir, self.state_dir)
        if not net.get("configured"):
            body = '<div class="empty-state">No network interface configured yet.</div>'
        else:
            query = query or {}
            range_choice = (query.get("range", ["7d"])[0] if isinstance(query.get("range"), list)
                             else query.get("range", "7d")) or "7d"
            range_choice = range_choice if range_choice in ("7d", "30d") else "7d"
            series = net.get("daily_series_30d" if range_choice == "30d" else "daily_series_7d", [])
            chart_svg = charts.render_daily_bar_chart(series, width=760, height=230,
                                                        title="Daily download/upload")
            range_toggle = "".join(
                f'<a href="/traffic?range={r}" class="badge {"badge-ok" if r == range_choice else "badge-off"}" '
                f'style="margin-right:6px;text-decoration:none">{_t(label_key, lang)}</a>'
                for r, label_key in (("7d", "last_7_days"), ("30d", "last_30_days"))
            )
            bundle = net.get("traffic_analytics", {})
            period_labels = [
                ("today", "today"), ("yesterday", "yesterday"), ("current_week", "this_week"),
                ("last_7_days", "last_7_days"), ("current_gregorian_month", "this_month_gregorian"),
                ("current_jalali_month", "this_month_jalali"), ("all_time", "all_time"),
            ]
            badge_class = {"FULL": "badge-ok", "PARTIAL_COVERAGE": "badge-warn"}
            badge_text = {"FULL": _t("full_coverage", lang), "PARTIAL_COVERAGE": _t("partial", lang),
                          "NO_DATA": _t("no_data", lang)}
            rows = ""
            for key, label_key in period_labels:
                p = bundle.get(key)
                if not p:
                    continue
                status = p.get("coverage_status", "")
                block = tf.format_traffic_block(p.get("rx_bytes"), p.get("tx_bytes"))
                rows += (f"<tr><td>{_html_escape(_t(label_key, lang))}</td>"
                         f"<td class='num'>{block['download']}</td>"
                         f"<td class='num'>{block['upload']}</td>"
                         f"<td class='num'><b>{block['combined']}</b></td>"
                         f"<td><span class='badge {badge_class.get(status, 'badge-off')}'>"
                         f"{badge_text.get(status, status.lower() if status else 'n/a')}</span></td></tr>")
            body = (f'<div class="card muted">{_t("interface", lang)}: {_html_escape(net["interface"])}. '
                    f'Historical samples are real measured data -- missing or partial periods are shown honestly, '
                    f'never implied as continuous coverage.</div>'
                    f'<div class="section-title">{_t("daily_history", lang)} &nbsp; {range_toggle}</div>'
                    f'<div class="card chart-wrap">{chart_svg}</div>'
                    f'<div class="section-title">{_t("standard_periods", lang)}</div>'
                    f'<div class="table-wrap"><table><tr><th>{_t("period", lang)}</th><th>{_t("download", lang)}</th>'
                    f'<th>{_t("upload", lang)}</th><th>{_t("combined", lang)}</th><th>{_t("coverage", lang)}</th></tr>'
                    f'{rows or "<tr><td colspan=5>No samples yet.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Traffic Analytics", body, lang, theme, "traffic"))

    def _page_inventory(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        from decimal import Decimal
        overview = api.get_vps_inventory_overview(self.state_dir)
        records = overview["records"]
        fleet = overview["fleet"]
        status_badge = {"running": "badge-ok", "stopped": "badge-off", "suspended": "badge-bad"}

        def _is_unlimited(q):
            try:
                return Decimal(str(q)) <= 0
            except Exception:
                return False

        rows = ""
        for r in records[:200]:
            status = r.get("runtime_status") or "unknown"
            quota = r.get("quota_gb")
            used = r.get("used_gb")
            unlimited = _is_unlimited(quota)
            if unlimited:
                limit_cell = f'<span class="badge badge-info">{_t("unlimited", lang)}</span>'
                pct_cell = '<span class="muted">n/a</span>'
            else:
                try:
                    limit_cell = f"{Decimal(str(quota)):.2f} GB"
                except Exception:
                    limit_cell = "unavailable"
                try:
                    pct = float(Decimal(str(used)) / Decimal(str(quota)) * 100) if quota and used not in (None, "unavailable") else None
                except Exception:
                    pct = None
                if pct is None:
                    pct_cell = '<span class="muted">unavailable</span>'
                else:
                    gclass = "g-bad" if pct >= 90 else "g-warn" if pct >= 75 else "g-ok"
                    pct_cell = (f'<div class="gauge-label"><span>{pct:.1f}%</span></div>'
                                f'<div class="gauge {gclass}"><span style="width:{min(pct,100):.1f}%"></span></div>')
            try:
                used_cell = f"{Decimal(str(used)):.2f} GB"
            except Exception:
                used_cell = "unavailable"
            status_display = _t(status, lang) if status in ("running", "stopped", "suspended") else status
            rows += (f'<tr><td><a href="/vps/{r["vpsid"]}">{r["vpsid"]}</a></td>'
                      f'<td>{_html_escape(r.get("hostname"))}</td>'
                      f'<td>{_html_escape(r.get("ip_primary"))}</td>'
                      f'<td><span class="badge {status_badge.get(status, "badge-off")}">{_html_escape(status_display)}</span></td>'
                      f'<td class="num">{used_cell}</td>'
                      f'<td class="num">{limit_cell}</td>'
                      f'<td style="min-width:110px">{pct_cell}</td></tr>')
        body = (f'<div class="card">{_t("fleet", lang)}: {fleet["total_vps"]} VPS total &nbsp; '
                f'<span class="badge badge-ok">{fleet.get("running_count", 0)} {_t("running", lang)}</span> &nbsp; '
                f'<span class="badge badge-off">{fleet.get("stopped_count", 0)} {_t("stopped", lang)}</span> &nbsp; '
                f'<span class="badge badge-bad">{fleet.get("suspended_count", 0)} {_t("suspended", lang)}</span></div>'
                f'<div class="table-wrap"><table><tr><th>{_t("vps_id", lang)}</th><th>{_t("name", lang)}</th>'
                f'<th>{_t("ip", lang)}</th><th>{_t("status", lang)}</th>'
                f'<th>{_t("used", lang)}</th><th>{_t("limit", lang)}</th><th>{_t("usage_pct", lang)}</th></tr>'
                f'{rows or "<tr><td colspan=7>No inventory synced yet.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("VPS Inventory", body, lang, theme, "inventory"))

    def _page_vps_detail(self, user, vpsid_raw, lang, theme) -> WebResponse:
        import control_center_api as api
        import traffic_format as tf
        from decimal import Decimal
        try:
            vpsid = int(vpsid_raw)
        except ValueError:
            return self._not_found()
        overview = api.get_vps_inventory_overview(self.state_dir)
        record = next((r for r in overview["records"] if r["vpsid"] == vpsid), None)
        if record is None:
            body = '<div class="empty-state">No such VPS in the current inventory snapshot.</div>'
        else:
            status = record.get("runtime_status") or "unknown"
            status_badge = {"running": "badge-ok", "stopped": "badge-off", "suspended": "badge-bad"}
            quota, used = record.get("quota_gb"), record.get("used_gb")
            try:
                unlimited = Decimal(str(quota)) <= 0
            except Exception:
                unlimited = False
            try:
                used_str = f"{Decimal(str(used)):.2f} GB"
            except Exception:
                used_str = "unavailable"
            if unlimited:
                limit_str, pct_block = _t("unlimited", lang), '<span class="muted">n/a</span>'
            else:
                try:
                    limit_str = f"{Decimal(str(quota)):.2f} GB"
                    pct = float(Decimal(str(used)) / Decimal(str(quota)) * 100)
                    gclass = "g-bad" if pct >= 90 else "g-warn" if pct >= 75 else "g-ok"
                    pct_block = (f'<div class="gauge-label"><span>{pct:.1f}%</span></div>'
                                 f'<div class="gauge {gclass}"><span style="width:{min(pct,100):.1f}%"></span></div>')
                except Exception:
                    limit_str, pct_block = "unavailable", f'<span class="muted">{_t("unavailable", lang)}</span>'
            status_display = _t(status, lang) if status in ("running", "stopped", "suspended") else status
            kpis = f"""
<div class="kpi"><div class="kpi-label">{_t('status', lang)}</div><div class="kpi-value">
<span class="badge {status_badge.get(status, 'badge-off')}">{_html_escape(status_display)}</span></div></div>
<div class="kpi"><div class="kpi-label">{_t('used', lang)}</div><div class="kpi-value">{used_str}</div></div>
<div class="kpi"><div class="kpi-label">{_t('limit', lang)}</div><div class="kpi-value">{_html_escape(limit_str)}</div></div>
<div class="kpi"><div class="kpi-label">{_t('usage_pct', lang)}</div><div class="kpi-value" style="font-size:16px">{pct_block}</div></div>
"""
            detail_rows = "".join(f"<tr><td>{_html_escape(k)}</td><td>{_html_escape(v)}</td></tr>"
                                   for k, v in record.items())
            body = (f'<div class="card"><b>VPS {vpsid}</b> &nbsp; {_html_escape(record.get("hostname"))} '
                    f'&nbsp; <span class="muted">{_html_escape(record.get("ip_primary"))}</span></div>'
                    f'<div class="kpi-grid">{kpis}</div>'
                    f'<div class="section-title">All fields (read-only snapshot -- no mutating actions here)</div>'
                    f'<div class="table-wrap"><table>{detail_rows}</table></div>')
        return WebResponse(body=self._render_page(f"VPS {vpsid_raw}", body, lang, theme, "inventory"))

    def _page_renewals(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        overview = api.get_renewal_overview(self.config_dir, self.state_dir)
        rows = ""
        for node_id, info in overview.get("per_node", {}).items():
            if not info.get("initialized"):
                rows += (f"<tr><td>{_html_escape(node_id)}</td>"
                         f'<td colspan=2><span class="badge badge-off">not initialized</span></td></tr>')
                continue
            unresolved = info["operations_unresolved"]
            rows += (f"<tr><td>{_html_escape(node_id)}</td><td class='num'>{info['operations_total']}</td>"
                      f"<td class='num'><span class=\"badge {'badge-warn' if unresolved else 'badge-ok'}\">"
                      f"{unresolved}</span></td></tr>")
        write_approved = overview.get('write_approved')
        auto_renewal = overview.get('auto_renewal_enabled')
        body = f"""
<div class="kpi-grid">
<div class="kpi {'kpi-ok' if write_approved else 'kpi-warn'}"><div class="kpi-label">{_t('write_approved_label', lang)}</div>
<div class="kpi-value">{write_approved}</div></div>
<div class="kpi {'kpi-ok' if auto_renewal else ''}"><div class="kpi-label">{_t('auto_renewal_label', lang)}</div>
<div class="kpi-value">{auto_renewal}</div></div>
<div class="kpi"><div class="kpi-label">{_t('monthly_grant_label', lang)}</div><div class="kpi-value">{overview.get('monthly_grant_gb')} GB</div></div>
</div>
<div class="section-title">{_t('per_node_operations', lang)}</div>
<div class="table-wrap"><table><tr><th>{_t('node', lang)}</th><th>{_t('total_ops', lang)}</th><th>{_t('unresolved', lang)}</th></tr>{rows}</table></div>
"""
        return WebResponse(body=self._render_page("Renewal Operations", body, lang, theme, "renewals"))

    def _page_emergency(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
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
                    message = '<p class="badge badge-ok">Saved.</p>'
                except ValueError as e:
                    message = f'<p class="badge badge-bad">Rejected: {_html_escape(e)}</p>'

        overview = api.get_emergency_policy_overview(self.config_dir, self.state_dir)
        csrf = self._csrf_field(token)
        options = "".join(f'<option value="{m}"{" selected" if m == overview["mode"] else ""}>{m}</option>'
                           for m in emp.VALID_MODES)
        admin_controls = (f'<form method="post">{csrf}<select name="mode">{options}</select>'
                           f'<button type="submit">{_t("save_mode", lang)}</button></form>'
                           if self._require_admin(user) else "")
        grants_marker = overview['emergency_grants_approved']
        body = f"""
<div class="card">{message}
<div class="kpi-grid">
<div class="kpi kpi-info"><div class="kpi-label">{_t('current_mode', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(overview['mode'])}</div></div>
<div class="kpi"><div class="kpi-label">{_t('grant_amount_label', lang)}</div><div class="kpi-value">{overview['emergency_grant_amount_gb']} GB</div></div>
<div class="kpi {'kpi-ok' if grants_marker else ''}"><div class="kpi-label">{_t('grants_approved_marker', lang)}</div>
<div class="kpi-value">{grants_marker}</div></div>
<div class="kpi {'kpi-warn' if overview['proposed_grants'] else 'kpi-ok'}"><div class="kpi-label">{_t('proposed_grants', lang)}</div>
<div class="kpi-value">{len(overview['proposed_grants'])}</div></div>
</div>
{admin_controls}</div>
"""
        return WebResponse(body=self._render_page("Emergency Bandwidth", body, lang, theme, "emergency"))

    def _page_review(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
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
                    message = '<p class="badge badge-ok">Resolved.</p>'
                elif review_id and action == "acknowledge":
                    emp.acknowledge_review(conn, int(review_id), acknowledged_by=user.username)
                    self._audit(user.username, "manual_review_acknowledged", detail=review_id,
                                remote_addr=remote_addr)
                    message = '<p class="badge badge-ok">Acknowledged.</p>'
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
            rows += (f'<tr><td>{item["id"]}</td><td><a href="/vps/{item["vpsid"]}">{item["vpsid"]}</a></td>'
                      f'<td>{_html_escape(item["reason"])}</td>'
                      f'<td><span class="badge badge-warn">{_html_escape(item["status"])}</span></td>'
                      f'<td>{controls}</td></tr>')
        body = (f'<div class="card muted">{len(pending)} pending review(s). Resolving/acknowledging requires '
                f'admin role and a valid CSRF token; never a destructive VPS action by itself.</div>'
                f'{message}<div class="table-wrap"><table>'
                f'<tr><th>{_t("id", lang)}</th><th>{_t("vps", lang)}</th><th>{_t("reason", lang)}</th>'
                f'<th>{_t("status", lang)}</th><th></th></tr>'
                f'{rows or "<tr><td colspan=5>No pending reviews.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Manual Review", body, lang, theme, "review"))

    def _page_alerts(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import control_center_api as api
        overview = api.get_emergency_policy_overview(self.config_dir, self.state_dir)
        rows = "".join(f'<tr><td><span class="badge badge-warn">review</span></td>'
                        f'<td><a href="/vps/{r["vpsid"]}">{r["vpsid"]}</a></td>'
                        f'<td>{_html_escape(r["reason"])}</td></tr>'
                        for r in overview["pending_reviews"])
        rows += "".join(f'<tr><td><span class="badge badge-info">grant</span></td>'
                         f'<td><a href="/vps/{g["vpsid"]}">{g["vpsid"]}</a></td>'
                         f'<td>+{g["amount_gb"]} GB proposed</td></tr>'
                         for g in overview["proposed_grants"])
        n_alerts = len(overview["pending_reviews"]) + len(overview["proposed_grants"])
        body = (f'<div class="kpi-grid"><div class="kpi {"kpi-warn" if n_alerts else "kpi-ok"}">'
                f'<div class="kpi-label">{_t("active_alerts", lang)}</div><div class="kpi-value">{n_alerts}</div></div></div>'
                f'<div class="table-wrap"><table><tr><th>{_t("type", lang)}</th><th>{_t("vps", lang)}</th>'
                f'<th>{_t("detail", lang)}</th></tr>'
                f'{rows or "<tr><td colspan=3>No active alerts.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Alerts", body, lang, theme, "alerts"))

    def _page_reports(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
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
                message = '<p class="badge badge-warn">Telegram bot is not enabled/configured with admins.</p>'
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
                    message = f'<p class="badge badge-ok">Delivery: {_html_escape(delivery)}</p>'
                except Exception as e:
                    message = f'<p class="badge badge-bad">Failed: {_html_escape(e)}</p>'
        last_sent = reports_mod.load_last_report_sent_date(self.state_dir)
        settings = tc.load_telegram_settings(self.config_dir)
        csrf = self._csrf_field(token)
        admin_controls = (f'<form method="post">{csrf}<button type="submit">{_t("send_report_now", lang)}</button></form>'
                           if self._require_admin(user) else "")
        body = f"""
<div class="card">{message}
<div class="kpi-grid">
<div class="kpi {'kpi-ok' if settings.reports_enabled else ''}"><div class="kpi-label">{_t('scheduled', lang)}</div>
<div class="kpi-value">{settings.reports_enabled}</div></div>
<div class="kpi"><div class="kpi-label">{_t('schedule', lang)}</div>
<div class="kpi-value" style="font-size:16px">{settings.report_hour:02d}:{settings.report_minute:02d}</div>
<div class="kpi-sub">{_html_escape(settings.report_timezone)}</div></div>
<div class="kpi"><div class="kpi-label">{_t('last_sent', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(last_sent or 'never')}</div></div>
</div>
{admin_controls}</div>
"""
        return WebResponse(body=self._render_page("Reports", body, lang, theme, "reports"))

    def _page_telegram(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import telegram_config as tc
        settings = tc.load_telegram_settings(self.config_dir)
        secrets_configured = os.path.exists(tc.default_secrets_path(self.config_dir))
        body = f"""
<div class="kpi-grid">
<div class="kpi {'kpi-ok' if settings.enabled else ''}"><div class="kpi-label">{_t('enabled', lang)}</div><div class="kpi-value">{settings.enabled}</div></div>
<div class="kpi kpi-info"><div class="kpi-label">{_t('connectivity_mode', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(settings.connectivity_mode)}</div></div>
<div class="kpi"><div class="kpi-label">{_t('admins_viewers', lang)}</div><div class="kpi-value">{len(settings.admin_ids)} / {len(settings.viewer_ids)}</div></div>
<div class="kpi {'kpi-ok' if secrets_configured else 'kpi-warn'}"><div class="kpi-label">{_t('secrets_configured', lang)}</div><div class="kpi-value">{secrets_configured}</div></div>
</div>
<div class="card muted">Edit via the CLI menu's "Configure Telegram bot" screen -- this page is read-only by
design (credential rotation/entry never happens over the Web UI or in chat). Polling/activation stays gated
until a real, verified connectivity check succeeds through the configured transport.</div>
"""
        return WebResponse(body=self._render_page("Telegram Settings", body, lang, theme, "telegram"))

    def _backup_root(self) -> str:
        return os.path.join(os.path.dirname(self.state_dir), "backups")

    def _page_backups(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
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
                message = (f'<p class="badge badge-ok">Created: {_html_escape(os.path.basename(archive))} '
                           f'(integrity: {"ok" if verification["ok"] else verification})</p>')
            except Exception as e:
                message = f'<p class="badge badge-bad">Backup failed: {_html_escape(str(e))}</p>'

        archives = backup_mod.list_backup_archives(backup_root)
        rows = "".join(f"<tr><td>{_html_escape(a)}</td></tr>" for a in reversed(archives))
        csrf = self._csrf_field(token)
        admin_controls = (f'<form method="post">{csrf}'
                           f'<button type="submit">{_t("create_backup_now", lang)}</button></form>'
                           if self._require_admin(user) else "")
        body = f"""
<div class="card">{message}
<div class="kpi-grid"><div class="kpi"><div class="kpi-label">{_t('archives_on_disk', lang)}</div><div class="kpi-value">{len(archives)}</div></div></div>
{admin_controls}
<p class="muted">Archives bundle all known state databases plus non-secret config
(never credentials, tokens, or password hashes). Encryption and scheduled/retention
backups are available via the CLI: <code>renewal_cli.py backup-full --encrypt --keep N</code>
(passphrase via the <code>TIXO_BACKUP_PASSPHRASE</code> environment variable, never a CLI
argument). Restoration always extracts to a staging directory for manual review -- it never
overwrites live data automatically.</p>
<div class="table-wrap"><table><tr><th>{_t('archive', lang)}</th></tr>{rows or '<tr><td class="muted">No backups yet.</td></tr>'}</table></div>
</div>
"""
        return WebResponse(body=self._render_page("Backups", body, lang, theme, "backups"))

    def _page_settings(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
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
                message = f'<p class="badge badge-ok">{_t("saved", lang)}</p>'
            except ValueError as e:
                message = f'<p class="badge badge-bad">Rejected: {_html_escape(e)}</p>'

        current_tz = menu_mod.load_display_tz(self.config_dir)
        current_pref = ht_mod.load_calendar_preference(self.config_dir)
        csrf = self._csrf_field(token)
        options = "".join(f'<option value="{p}"{" selected" if p == current_pref else ""}>{p}</option>'
                           for p in ht_mod.VALID_CALENDAR_PREFERENCES)
        admin_form = (f'<form method="post">{csrf}'
                      f'<p>{_t("display_timezone", lang)}: <input name="display_tz" value="{_html_escape(current_tz)}"></p>'
                      f'<p>{_t("calendar_preference", lang)}: <select name="calendar_preference">{options}</select></p>'
                      f'<button type="submit">{_t("save", lang)}</button></form>' if self._require_admin(user) else "")
        body = f"""
<div class="card">{message}
<div class="kpi-grid">
<div class="kpi"><div class="kpi-label">{_t('display_timezone', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(current_tz)}</div></div>
<div class="kpi"><div class="kpi-label">{_t('calendar_preference', lang)}</div><div class="kpi-value" style="font-size:16px">{_html_escape(current_pref)}</div></div>
</div>
{admin_form}</div>
"""
        return WebResponse(body=self._render_page("System Settings", body, lang, theme, "settings"))

    def _page_users(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
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
                    message = '<p class="badge badge-ok">User created.</p>'
                elif action == "disable":
                    wa.set_user_disabled(self.config_dir, form.get("username", ""), True)
                    self._audit(user.username, "user_disabled", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = '<p class="badge badge-ok">User disabled.</p>'
                elif action == "enable":
                    wa.set_user_disabled(self.config_dir, form.get("username", ""), False)
                    self._audit(user.username, "user_enabled", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = '<p class="badge badge-ok">User enabled.</p>'
                elif action == "delete":
                    wa.delete_user(self.config_dir, form.get("username", ""))
                    self._audit(user.username, "user_deleted", detail=form.get("username"),
                                remote_addr=remote_addr)
                    message = '<p class="badge badge-ok">User deleted.</p>'
            except ValueError as e:
                message = f'<p class="badge badge-bad">Rejected: {_html_escape(e)}</p>'

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
                            f'<button name="form_action" value="{toggle_action}" class="secondary">{toggle_action}</button></form> '
                            f'<form method="post" style="display:inline">{csrf}'
                            f'<input type="hidden" name="username" value="{_html_escape(u.username)}">'
                            f'<button name="form_action" value="delete">delete</button></form>')
            rows += (f'<tr><td>{_html_escape(u.username)}</td>'
                      f'<td><span class="badge {"badge-info" if u.role == "admin" else "badge-off"}">{_html_escape(u.role)}</span></td>'
                      f'<td><span class="badge {"badge-bad" if u.disabled else "badge-ok"}">{u.disabled}</span></td>'
                      f'<td>{controls}</td></tr>')
        create_form = (f'<form method="post">{csrf}<input type="hidden" name="form_action" value="create">'
                       f'<input name="username" placeholder="username">'
                       f'<input name="password" type="password" placeholder="password">'
                       f'<select name="role"><option value="viewer">viewer</option>'
                       f'<option value="admin">admin</option></select>'
                       f'<button type="submit">{_t("create", lang)}</button></form>' if is_admin else "")
        body = (f'<div class="card">{message}'
                f'<div class="table-wrap"><table><tr><th>{_t("user", lang)}</th><th>{_t("role", lang)}</th>'
                f'<th>{_t("disabled", lang)}</th><th></th></tr>{rows}</table></div>{create_form}</div>')
        return WebResponse(body=self._render_page("User Management", body, lang, theme, "users"))

    def _page_audit(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import audit_log
        conn = audit_log.open_db(self._audit_db_path())
        try:
            events = audit_log.list_recent_events(conn, limit=100)
        finally:
            conn.close()
        rows = "".join(f'<tr><td class="muted">{_html_escape(e["occurred_at"])}</td><td>{_html_escape(e["actor"])}</td>'
                        f'<td><span class="badge badge-off">{_html_escape(e["action"])}</span></td>'
                        f'<td>{_html_escape(e["detail"])}</td></tr>'
                        for e in events)
        body = (f'<div class="card muted">Most recent {len(events)} event(s) (newest first). Every mutating '
                f'admin action on this panel is recorded here.</div>'
                f'<div class="table-wrap"><table><tr><th>{_t("time", lang)}</th><th>{_t("actor", lang)}</th>'
                f'<th>{_t("action", lang)}</th><th>{_t("detail", lang)}</th></tr>'
                f'{rows or "<tr><td colspan=4>No events recorded yet.</td></tr>"}</table></div>')
        return WebResponse(body=self._render_page("Audit Logs", body, lang, theme, "audit"))

    def _page_health(self, user, method, form, token, remote_addr, lang, theme, query=None) -> WebResponse:
        import menu as menu_mod
        timers = menu_mod.collect_timer_status()
        rows = ""
        n_active = n_failed = 0
        for unit, info in timers.items():
            health = menu_mod.classify_timer_health(info.get("active", ""), info.get("enabled", ""))
            badge = "badge-ok" if health == "ACTIVE" else ("badge-bad" if health == "FAILED" else "badge-off")
            if health == "ACTIVE":
                n_active += 1
            elif health == "FAILED":
                n_failed += 1
            rows += (f'<tr><td>{_html_escape(unit)}</td>'
                      f'<td><span class="badge {badge}">{health}</span></td></tr>')
        summary = f"""
<div class="kpi-grid">
<div class="kpi kpi-info"><div class="kpi-label">{_t('timers_tracked', lang)}</div><div class="kpi-value">{len(timers)}</div></div>
<div class="kpi kpi-ok"><div class="kpi-label">{_t('active', lang)}</div><div class="kpi-value">{n_active}</div></div>
<div class="kpi {'kpi-bad' if n_failed else 'kpi-ok'}"><div class="kpi-label">{_t('failed', lang)}</div><div class="kpi-value">{n_failed}</div></div>
</div>
"""
        body = summary + (f'<div class="table-wrap"><table><tr><th>{_t("timer", lang)}</th>'
                           f'<th>{_t("health", lang)}</th></tr>{rows}</table></div>')
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
