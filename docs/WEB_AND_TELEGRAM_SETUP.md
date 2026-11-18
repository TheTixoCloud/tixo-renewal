# Web Control Center and Telegram Bot Setup Guide

Both modules are **disabled by default** and never activate just because configuration or
secrets happen to exist — each has its own explicit `enabled` flag and refuses to start its
process without it.

**Verification status:** all local behavior (auth, CSRF, RBAC, audit, rendering, live
monitoring, TLS) is verified with real local execution, including a real end-to-end test
against the actual built package over a genuine loopback socket. No real Telegram API
connectivity and no real two-host relay validation have been performed — see
`REMOTE_AND_RELAY.md`.

## Web Control Center

### Enable it

From the menu: **"Configure Web Control Center (optional)"** — sets bind host/port,
language (`en`/`fa`), theme (`dark`/`light`), domain, and optionally HTTPS (see below);
creates the first admin user if none exists.

### Start it

```bash
renewal_cli.py web-run                      # blocks forever (the real deployment shape)
renewal_cli.py web-run --max-iterations 5   # bounded, for manual smoke-testing
```
Refuses with `BLOCKED: ...` if not enabled.

### HTTPS

Two paths, both manual-certificate-path only (no automatic provisioning is wired into the
menu yet — `installer.request_automatic_certificate()` exists and is unit-tested via
dependency injection, but has never actually executed a real `certbot` call against a real
domain):

1. **Manual certificate paths** (recommended, verified): the config screen prompts for
   `.pem`/`.crt` and key paths, validates that they load together and that the chosen port
   is free, and only then wires them into `WebSettings.tls_cert_path`/`tls_key_path`.
   `web-run` wraps the listening socket in real TLS; the session cookie is marked `Secure`
   automatically whenever TLS is active.
2. **Reverse proxy** (also fully supported, not this project's concern): put nginx/Caddy in
   front of plain-HTTP `127.0.0.1:<port>` and terminate TLS there instead. No worked
   examples are included in this repository yet.

### All 17 pages

Overview, Live Host Monitoring, Network Traffic, Traffic Analytics, VPS Inventory, VPS
Details, Renewal Operations, Emergency Bandwidth, Manual Review, Alerts, Reports, Telegram
Settings, Backups, System Settings, User Management, Audit Logs, Service Health — all wired
to the same shared service layer (`control_center_api.py`) the CLI/menu use, never a
separate or fabricated data path. A feature not yet implemented says so honestly on its
page rather than inventing a plausible-looking number.

### Security properties (verified)

- Session-cookie auth, server-side SQLite sessions, login rate limiting.
- RBAC: `admin` vs `viewer`, enforced on every mutating route.
- CSRF: every POST requires a per-session derived token.
- Audit log: every mutating action recorded.
- Live monitoring never calls the Virtualizor API (one raw kernel-counter read per poll).
- Request bodies are capped (2MB) before being read into memory.

## Telegram bot

### Enable it

From the menu: **"Configure Telegram bot (optional)"** — choose connectivity mode
(`direct`/`socks5`/`relay`), set admin/viewer numeric Telegram IDs, store the bot token
(and SOCKS5 password / relay shared secret if applicable), optionally run a connectivity
test (a real `getMe` call — only happens if you explicitly confirm it).

### Connectivity modes

- **`direct`** (default): plain HTTPS to `api.telegram.org`. Verified with mocked HTTP
  responses (never a real Telegram API call in this project's test suite or development
  history).
- **`socks5`**: a from-scratch SOCKS5 client (RFC 1928/1929) over a real local test SOCKS5
  server. Verified locally; never tested against a real third-party proxy or real
  Telegram endpoint.
- **`relay`**: requires a SEPARATELY deployed relay worker (its own host, its own
  `renewal_cli.py relay-worker-run`, enabled via its own "Configure Relay Worker" screen).
  See `REMOTE_AND_RELAY.md` — **this mode has only been validated against a simulated
  second host on localhost, never two genuinely separate machines.**

### Start the bot

```bash
renewal_cli.py telegram-bot-run
```
Refuses with `BLOCKED: ...` unless enabled and secrets load successfully.

### Commands, authorization, rate limiting

Numeric Telegram user ID only (never username-based) checked against `admin_ids`/
`viewer_ids`; every update rate-limited before any command logic runs. Scheduled daily
reports are checked once per polling-loop iteration (no separate timer).

### systemd unit

`virt-renewal-telegram-bot.service` — `Type=simple`, `Restart=always` (a persistent
long-poll process, not a periodic timer). Generated at install time, never installed or
enabled automatically.
