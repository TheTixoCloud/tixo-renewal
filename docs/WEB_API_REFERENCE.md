# Web Control Center — Route Reference

All routes are served by `WebApp.handle_request()` in `implementation/web_server.py`. There
is no separate REST/JSON API beyond `/api/live` — every other route returns a full HTML page
(server-rendered, no client-side framework). Default bind: `127.0.0.1:18443` (localhost-only
until an operator explicitly reconfigures `bind_host` and provisions TLS).

## Authentication & session

- `POST /login` — username/password, bcrypt-style hashed comparison (`web_auth.verify_login`),
  rate-limited (10 attempts / 5 minutes per remote address → HTTP 429), sets an `HttpOnly`
  session cookie (`Secure` flag added automatically once TLS is configured).
- `POST /logout` — destroys the session.
- Every other route requires a valid session cookie; an unauthenticated request is redirected
  (`302`) to `/login`.
- `GET /static/<file>`, `GET /favicon.ico` — the only unauthenticated routes besides
  `/login` itself; served from a fixed allowlist dict (`STATIC_FILES`), never a filesystem
  path built from user input.

## Authorization

Two roles: `admin` and `viewer`. All `GET` routes are readable by both. Every `POST` (every
mutating action) additionally requires `role == "admin"` (`WebApp._require_admin`) **and** a
valid CSRF token (`WebApp._check_csrf`, derived from the session via
`web_auth.derive_csrf_token`) — missing/invalid either one returns `403`.

## Pages (GET, all require login)

| Route | Page | Notes |
|---|---|---|
| `/` | Overview | Fleet/host KPIs, per-period traffic KPIs, live bandwidth widget, 7-day chart |
| `/host` | Live Host Monitoring | CPU/memory/host detail, live reading poller |
| `/network` | Network Traffic | Persistent cumulative totals, live bandwidth widget |
| `/traffic` | Traffic Analytics | Standard-period table + 7d/30d historical chart (`?range=7d\|30d`) |
| `/inventory` | VPS Inventory | All synced VPS records, compact adaptive table |
| `/vps/<id>` | VPS Detail | Single VPS, KPI header + full read-only field dump |
| `/renewals` | Renewal Operations | Per-node operation counts, write-approval/auto-renewal state |
| `/emergency` | Emergency Bandwidth | Current policy mode (admin `POST` to change it) |
| `/review` | Manual Review | Pending exhausted-bandwidth reviews (admin `POST` to ack/resolve) |
| `/alerts` | Alerts | Merged view of pending reviews + proposed grants |
| `/reports` | Reports | Schedule status (admin `POST` to send one now) |
| `/telegram` | Telegram Settings | Read-only — connectivity mode, admin/viewer counts, no secrets |
| `/backups` | Backups | Archive list (admin `POST` to create one now) |
| `/settings` | System Settings | Display timezone / calendar preference (admin `POST` to change) |
| `/users` | User Management | Admin/viewer accounts (admin `POST` to create/disable/delete) |
| `/audit` | Audit Logs | Last 100 mutating-action events |
| `/health` | Service Health | The five renewal timers' active/failed state |

## JSON API

```
GET /api/live
```
Requires login (same as every page). Returns:
```json
{
  "configured": true,
  "interface": "ens1f1",
  "ok": true,
  "rx_bytes": 249893697162653,
  "tx_bytes": 254592532912160,
  "read_at": "2026-11-18T21:24:45.736702+00:00",
  "download": "249.89 TB",
  "upload": "254.59 TB",
  "combined": "504.49 TB"
}
```
One `/sys/class/net` read per request — no rate itself is computed server-side. `rx_bytes`/
`tx_bytes` are raw cumulative kernel counters; `download`/`upload`/`combined` are the same
values decimal-formatted via `traffic_format.format_traffic_block()`. The live-bandwidth
widget (`static/live-bandwidth.js`) polls this every ~4s and computes a RATE client-side
from the delta between consecutive `rx_bytes`/`tx_bytes`/`read_at` triples — see
`docs/RELEASE_NOTES_v1.10.0.md` for the full design rationale.

If `configured` is `false`, no interface has been chosen yet (via `hostnet-configure`) —
every numeric field is omitted, never fabricated as zero.

## Localization & theme

Two cookies control presentation, read on every request, never required: `tixo_lang`
(`en`/`fa`) and `tixo_theme` (`dark`/`light`). Neither is session/security-relevant.

## What is deliberately NOT exposed over Web

No route can trigger `apply`/`apply-due` (an actual bandwidth renewal), change
`WRITE_APPROVED`, modify `policy.json`, or touch Virtualizor directly — those remain CLI-only,
consistent with the project's write-capability model (every write path requires explicit,
interactive, `--execute`-gated CLI confirmation; the Web UI is read-heavy with a narrow,
audited set of admin-only mutations listed above).
