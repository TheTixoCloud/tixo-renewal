# Database Schema Reference

All state lives in per-database SQLite files under `$TIXO_RENEWAL_INSTALL_DIR/shadow-monitor-state/`
(default `/opt/tixo-renewal/shadow-monitor-state/`). There are no separate migration files —
every module creates its own schema idempotently via `CREATE TABLE IF NOT EXISTS` at first use
(see `installer.cmd_install`/`install-upgrade`, which re-runs this on every upgrade without
touching existing data). This file is a reference, not a migration script; the actual `CREATE
TABLE` statements in each module below are the source of truth.

| Database file | Owning module | Purpose |
|---|---|---|
| `self.sqlite3` (per-node, named after the node id) | `shadow_monitor.py` | The renewal engine's own ledger: VPS bandwidth identities, monitoring checkpoints, renewal checkpoints, point-in-time snapshots, and identity-conflict records (VPS ID reuse detection). |
| `inventory.sqlite3` | `inventory.py` | VPS inventory snapshots (one row per sync), diffs between consecutive syncs, and the known-VPS table used for ID-reuse detection. |
| `hostnet.sqlite3` | `hostnet.py` | Host-level network accounting: cumulative RX/TX totals per interface, 5-minute-interval samples (with delta/gap/discontinuity classification), and interface source-change events (configured/renamed/replaced). |
| `emergency_policy.sqlite3` | `emergency_policy.py` | Exhausted-bandwidth manual-review queue and proposed/applied emergency grants. |
| `audit_log.sqlite3` | `audit_log.py` | Append-only record of every mutating Web/CLI admin action (actor, action, detail, remote address, timestamp). |
| `web_sessions.sqlite3` | `web_auth.py` | Active Web login sessions (token → username, expiry). |
| `web_login_ratelimit.sqlite3` | `security.py` (via `web_auth.py`) | Sliding-window login-attempt counters per remote address. |

## Tables by database

### `self.sqlite3` (shadow_monitor.py)
- `identities` — known VPS bandwidth identities (vpsid, first-seen quota/usage).
- `identity_conflicts` — detected VPS-ID-reuse events (a new VPS appearing at an ID previously
  assigned to a different VPS).
- `monitor_checkpoints` — last-observed state per VPS from the read-only monitor timer.
- `renewal_checkpoints` — last-applied renewal state per VPS (used to prevent double-renewal).
- `snapshots` — point-in-time full-fleet snapshots for historical comparison.

### `inventory.sqlite3` (inventory.py)
- `snapshots` — one row per inventory sync run (timestamp, node, completeness).
- `snapshot_records` — one row per VPS per snapshot (hostname, status, quota, used, IP, etc.).
- `known_vps` — the latest known state per VPS ID, used for ID-reuse detection.
- `inventory_events` — diff events between consecutive snapshots (added/removed/modified).

### `hostnet.sqlite3` (hostnet.py)
- `hostnet_cumulative` — one row per interface: running cumulative RX/TX totals, first/last
  sample timestamps, last boot ID.
- `hostnet_samples` — one row per 5-minute sample: raw counter values, computed delta,
  gap_seconds, discontinuity_type (reboot/rollover/unknown/none).
- `hostnet_meta` — small key/value store (e.g. schema version markers).
- `hostnet_source_events` — interface configuration change history (configured/renamed/replaced),
  used to distinguish a genuine device replacement from a kernel rename.

### `emergency_policy.sqlite3` (emergency_policy.py)
- `manual_review_queue` — VPSs flagged for manual review on bandwidth exhaustion (mode =
  MANUAL_REVIEW), with resolve/acknowledge status.
- `emergency_grants` — proposed/applied emergency bandwidth grants (mode = EMERGENCY_GRANT),
  with cooldown and monthly-cap enforcement fields.

### `audit_log.sqlite3` (audit_log.py)
- `audit_events` — append-only: occurred_at, actor, action, detail, remote_addr.

### `web_sessions.sqlite3` (web_auth.py)
- `web_sessions` — session token (hashed), username, created_at, expires_at.

### `web_login_ratelimit.sqlite3` / `security.py`
- `rate_limit_events` — generic sliding-window counter table, keyed by an arbitrary bucket
  string (e.g. `login:<remote_addr>`, `telegram:<user_id>`) — shared by the Web login limiter
  and the Telegram bot's per-user rate limiter.

## Non-SQLite persistent state

- `renewal-config/*.json` — policy, nodes, web/telegram/backup settings (plain JSON, no
  schema versioning needed — each loader fills in defaults for missing keys).
- `renewal-config/web/users.json` — Web admin/viewer accounts (username → {password_hash,
  role, disabled}).
- `renewal-config/{web,telegram}/secrets.json`, `csrf_secret.key` — 0600-permission secret
  files, never included in any backup/archive created by `backup.create_full_backup_archive()`
  (see `backup.py`'s `ALLOWLISTED_CONFIG_FILES`) — only a full raw filesystem copy (as used for
  this project's own pre-upgrade backups) captures these.

## Schema evolution policy

Every schema change in this project so far has been strictly additive (`CREATE TABLE IF NOT
EXISTS`, new nullable columns via `_add_column_if_missing`-style helpers where needed) — there
has never been a destructive migration, and `installer.upgrade()`/`install-upgrade` is designed
around that assumption (it re-runs the same idempotent creation logic on every upgrade,
unconditionally safe to repeat). If a future change ever requires a genuinely destructive
migration (dropping/renaming a column, changing a primary key), it must ship with an explicit,
reversible migration function and a corresponding `install-repair`/`install-rollback` path —
there is no such path today because it has never been needed.
