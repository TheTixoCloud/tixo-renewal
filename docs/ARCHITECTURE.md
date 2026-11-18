# Architecture & Data Flow

## Deployment model

TixoCloud Control Center runs **directly on each Virtualizor host** (currently one host:
`ir1.tixocloud.com`). It reads Virtualizor's own local Admin API config (`universal.php`) the
same way Virtualizor's own admin scripts do — no new credentials invented, no SSH, no
dependency on any other server at runtime for the core renewal engine. There is no central
server and no agent inside any customer VPS. Zero third-party Python dependencies anywhere
(stdlib only: `sqlite3`, `json`, `decimal`, `subprocess`, `urllib`, `http.server`, `ssl`,
`socket`) — no pip, no venv required on the production host.

## Process model

Everything is a separate process, started/stopped independently, all optional except the
core renewal engine's read-only monitor timer:

| Process | Started by | Default state |
|---|---|---|
| `virt-renewal-monitor` | systemd timer (30 min) | Enabled, read-only (`dry-run` only) |
| `virt-renewal-apply` | systemd timer | Generated but never auto-installed; requires `WRITE_APPROVED` + explicit activation |
| `virt-renewal-inventory-sync` | systemd timer | Enabled — refreshes `inventory.sqlite3` |
| `virt-renewal-hostnet-sync` | systemd timer (5 min) | Enabled — samples the configured network interface |
| `virt-renewal-readiness-notifier` | systemd timer | Enabled — readiness/health signal |
| `virt-renewal-web` | systemd service (`Restart=always`) | Optional, operator-enabled |
| `virt-renewal-telegram-bot` | systemd service (`Restart=always`) | Optional, operator-enabled, requires verified SOCKS5/DIRECT/RELAY connectivity first |
| `virt-renewal-relay-worker` | systemd service | Optional, runs on a SEPARATE host (for Iran-side Telegram connectivity) |
| `virt-renewal-agent` | systemd service | Optional, read-only remote management agent |
| `virt-renewal-backup` | systemd timer | Optional, scheduled encrypted backups |

## Layering (strict, one-directional)

```
CLI (renewal_cli.py, menu.py)  ─┐
Web (web_server.py)            ─┼─► control_center_api.py ─► {inventory,hostnet,emergency_policy,
Telegram (telegram_bot.py)     ─┘                             shadow_monitor,analytics,...}.py
                                                                       │
                                                                       ▼
                                                              SQLite files (shadow-monitor-state/)
```

`control_center_api.py` is the single shared, **read-only** service layer every surface
(CLI/Web/Telegram) calls into for status/monitoring data — "avoid three independent
implementations of business logic" is an explicit project rule, enforced by convention (every
new Web page or Telegram command is required to call into this layer, never re-derive logic).
It never calls the Virtualizor Admin API directly — it only reads from the already-synced
local SQLite caches that the independent timers populate, so Web/Telegram/CLI request volume
can never increase real Virtualizor API load.

The one exception is `control_center_api.get_live_network_reading()` — a single
`/sys/class/net` read (not a network/API call), exactly as cheap as the CLI's own live-view
refresh.

## Write path (the one place that can mutate Virtualizor)

```
renewal_cli.py apply --execute  ──► policy.py (compute what's due)
                                  ──► writer.py (the ONLY module that calls Virtualizor's
                                       write API) ──► shadow_monitor.py (record checkpoint)
```

Gated by, in order: `renewal-config/WRITE_APPROVED` must exist (created manually, once,
deliberately), `--execute` flag, interactive confirmation (unless `--non-interactive`), valid
stored credentials (`store-credentials`, never created automatically). No other code path in
the entire project — not Web, not Telegram, not any read-only CLI command — can reach
`writer.py`.

## Shared formatting layer

`traffic_format.py` is the single place both the CLI (`menu.py`, via aliases) and the Web
Control Center convert raw byte counts/rates into human-readable strings — decimal (SI)
GB/TB for totals, bits/sec (Mbps/Gbps) for rates, a documented no-misleading-rounding fix,
and a `combine()` helper that never silently treats a missing RX/TX side as zero. See its
module docstring for the full unit-convention writeup.

## Web Control Center internals

`WebApp.handle_request()` is a pure, socket-free function (method/path/headers/cookies/body
→ status/headers/body) — this is what the entire test suite calls directly. `TixoRequestHandler`
is the only part that touches a real socket, translating a raw HTTP request into that same
call. TLS (when configured) wraps the listening socket via `ssl.SSLContext` +
`load_cert_chain()` — manual certificate paths only; the app never requests/provisions a
certificate itself (that's a separate, operator-triggered `certbot` step, see
`docs/DEPLOYMENT.md`).

## Telegram bot internals

`telegram_bot.process_update()` is the pure, network-independent dispatch function (a
Telegram "update" dict → `{reply, chat_id, authorized, rate_limited, command}`) — fully
testable with fake update dicts, never a real network call. `telegram_transport.py` provides
three interchangeable transports (`DirectTransport`, `Socks5Transport`,
`RelayClientTransport`) behind one interface; `telegram_bot.build_transport()` picks one
based on `settings.connectivity_mode` and raises rather than silently falling back to a less
secure mode if required config is missing.

## Live bandwidth monitoring (v1.10.0)

No new backend process. `static/live-bandwidth.js` (vanilla JS, zero dependencies) polls the
existing `/api/live` endpoint every ~4s and computes a rate client-side from the delta
between consecutive raw counter reads — bounded to a 300-sample ring buffer per widget
instance. See `docs/WEB_API_REFERENCE.md` and `docs/RELEASE_NOTES_v1.10.0.md`.

## Backup formats (two, deliberately different scopes)

1. `backup.create_full_backup_archive()` (used by `backup-full`, the Web `/backups` page, and
   `installer.upgrade()`'s automatic pre-upgrade backup) — all known SQLite databases (via
   `sqlite3.Connection.backup()`, consistent even under concurrent writes) plus an
   **allowlist** of non-secret config files. Deliberately **excludes** `secrets.json`,
   `users.json`, `csrf_secret.key`, and anything not explicitly allowlisted.
2. A full raw filesystem tar of the entire install directory (used for this project's own
   pre-upgrade/pre-patch production backups during this engagement, and recommended for any
   disaster-recovery backup) — captures everything including secrets, at the cost of needing
   its own encryption/access-control discipline (see `docs/BACKUP_AND_RESTORE.md`).
