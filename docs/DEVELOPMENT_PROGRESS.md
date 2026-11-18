# TixoCloud Control Center — Autonomous Development Progress

**Mission:** Transform Tixo Renewal into a full "TixoCloud Control Center" platform (CLI +
Telegram + Web, shared backend, local/remote install modes, emergency-bandwidth policy,
reporting, backups). See the full mission brief for the complete scope.

**Status as of this checkpoint:** Early, honest progress on a prioritized subset. This
document exists so a future session (human or agent) can resume without re-deriving context.
It intentionally does **not** claim anything is done that isn't actually implemented and
tested — see the mission's own instruction: *"A feature counts as complete only when its
actual behavior and tests are implemented."*

Base version at start of this work: **v1.7.3** (unchanged — see "Why the version wasn't
bumped" below).

---

## What is actually DONE (implemented + tested, real behavior)

### 1. Historical host-network traffic analytics — DONE
- **File:** `implementation/traffic_analytics.py` (new), small additive extension to
  `implementation/hostnet.py::get_samples_in_range` (added `gap_seconds` to its return dict —
  backward compatible, no existing caller broke).
- Provides: `today`, `yesterday`, `last_24_hours`, `current_week` (ISO Monday start),
  `last_7_days`, `current_gregorian_month`, `previous_gregorian_month`,
  `current_jalali_month`, `previous_jalali_month`, `all_time`, and `custom_range(start, end)`.
- Each period reports RX/TX/combined bytes, average throughput, peak instantaneous combined
  throughput (derived per-sample from the existing 5-minute sampler's own delta/gap, never a
  live ~1Hz reading), and an explicit `coverage_status` (`FULL` / `PARTIAL_COVERAGE` /
  `NO_DATA`) so "no data yet" is never visually indistinguishable from "genuinely zero
  traffic."
- Timezone-aware (default `Asia/Tehran`, matching `menu.py`'s existing default), reuses
  `jalali.py`'s existing month-boundary logic (no second independent Jalali implementation).
- Wired into the CLI as a new, read-only **"Host network traffic analytics (historical)"**
  menu screen (`menu.render_traffic_analytics_report` / `menu.action_view_traffic_analytics`).
- **Tests:** `tests/test_traffic_analytics.py` (22 tests) + 2 in `tests/test_menu.py` — date
  boundaries, Jalali-vs-Gregorian month divergence, rolling vs. calendar windows, missing-data
  coverage states, peak-vs-average calculation, custom-range future-clamping safety.

### 2. Exhausted-bandwidth policy foundation (emergency grant / manual review) — DONE
*(detection + policy + audit layer only — see "Deliberately NOT done" below for the boundary)*
- **File:** `implementation/emergency_policy.py` (new), own SQLite database
  (`emergency_policy.sqlite3`), own settings file (`emergency-policy-settings.json`), fully
  separate from the existing renewal ledger (`self.sqlite3`) and inventory cache
  (`inventory.sqlite3`) — read-only toward both, writes only to its own DB.
- **Default mode is `disabled`** everywhere (dataclass default, settings-file-absent
  fallback, malformed-file fallback) — i.e. **zero behavior change** for any existing
  installation until an operator explicitly opens "Configure exhausted-bandwidth policy" and
  changes it.
- Two mutually exclusive modes once enabled:
  - `emergency_grant`: proposes a small, configurable top-up (default 1 GB) when a VPS is
    exhausted and not already due for its regular scheduled renewal, subject to a
    configurable cooldown (default 7 days) and max-grants-per-calendar-month (default 1).
    Exceeding either limit is reported as **blocked**, never silently granted anyway.
  - `manual_review`: never auto-grants; records the exhaustion in a durable queue
    (vpsid, reason, detected_at, status) for a human to acknowledge and resolve, with full
    audit history (rows are never deleted, only status-transitioned).
- Idempotent: repeated detection calls for the same ongoing exhaustion episode never create a
  duplicate grant-proposal or queue entry; a new episode is only opened after the previous one
  reaches a terminal state (`APPLIED`/`EXPIRED` for grants, `RESOLVED` for reviews).
- Unlimited VPS (`quota_gb <= 0`) are never considered exhausted, matching the same rule
  already used in `analytics.py`/`policy.py`.
- Wired into the CLI as four new, read-mostly menu screens: **"Configure exhausted-bandwidth
  policy"**, **"Check exhausted VPS bandwidth"** (evaluates the *existing, already-synced*
  inventory cache — makes **no** Virtualizor API call of its own), **"Manual review queue"**
  (list/acknowledge/resolve), **"Emergency grants (proposed)"** (read-only list).
- **Tests:** `tests/test_emergency_policy.py` (27 tests) + 4 in `tests/test_menu.py` —
  disabled-by-default safety, idempotency, cooldown, monthly cap (including a real bug found
  and fixed during testing — see below), unlimited exclusion, review lifecycle, audit-trail
  immutability, settings round-trip.

  **Bug found and fixed while testing this module:** the first implementation of
  `_get_open_grant` treated an `APPLIED` grant as still "open" forever, which would have
  permanently blocked cooldown/max-per-month evaluation for any VPS that ever received one
  grant. Fixed by narrowing "open" to mean `PROPOSED` only; `APPLIED`/`EXPIRED` are terminal
  and fall through to the cooldown/cap history checks instead. Caught by
  `test_cooldown_allows_grant_after_window_elapses` and three sibling tests before this was
  ever wired to anything live.

### 3. Emergency grant → writer.py apply wiring — DONE (this checkpoint)
- **Function:** `emergency_policy.apply_proposed_grant()`. Reuses `writer.apply_renewal()`
  **completely unchanged** — no new write implementation, no duplicated structural gate.
  Every existing safety property (unresolved-prior-operation dedup, write-cooldown, the
  `<=0`-means-unlimited guard, pre/post-write live verification reads, the false-success
  settle-delay re-read) applies exactly as it does to a regular scheduled renewal.
- **New, separate authorization gate, exactly as required:** `EMERGENCY_GRANTS_APPROVED`
  (a second marker file in `renewal-config/`, parallel to `WRITE_APPROVED`). Both markers
  are required; **the pre-existing `WRITE_APPROVED` marker alone can never activate this
  feature** — verified by a dedicated test
  (`test_emergency_grants_require_separate_approval_marker_write_approved_alone_insufficient`)
  that confirms zero ledger operations are created when only `WRITE_APPROVED` exists.
- **Idempotency is writer.py's own mechanism, not reimplemented:** `period_key =
  f"emergency-grant:{grant_id}"` is unique per grant row, so `find_any_prior_operation()`
  inside `apply_renewal()` already blocks a double-apply for the same grant — proven by
  `test_emergency_grant_duplicate_apply_blocked_by_writers_own_idempotency`.
- The new quota written is **current live quota + grant amount** (an increment freshly read
  immediately before computing the target), never a stale/absolute recomputation — proven by
  `test_emergency_grant_new_quota_is_increment_not_absolute`.
- A grant is only marked `APPLIED` in `emergency_policy`'s own table when writer.py's result
  status is the literal string `"APPLIED"` — any other outcome (blocked, unverified, etc.)
  leaves it `PROPOSED` for a future retry, never falsely marked.
- Wired into the CLI: "Emergency grants (proposed)" now offers an explicit apply action,
  mirroring `renewal_cli.cmd_apply`'s own interactive shape (type-`APPLY` confirmation,
  live-typed credentials, never stored) — and refuses even to prompt for credentials if
  `EMERGENCY_GRANTS_APPROVED` is absent (verified by
  `test_action_view_emergency_grants_blocks_apply_without_approval_marker`, which asserts
  `input()` is never called at all in that case).
- **Tests:** 13 new in `tests/test_emergency_policy.py` (using the REAL `writer.apply_renewal`
  with mocked HTTP/read functions, the same fixture pattern `tests/test_writer.py` already
  uses — never a second, simplified fake of the write path) + 1 in `tests/test_menu.py`.

### 4. Shared backend service layer + security primitives — DONE (this checkpoint)
- **Files:** `implementation/control_center_api.py`, `implementation/security.py` (both new).
- **control_center_api.py** is the single module any future Telegram command or Web endpoint
  must call into for monitoring/status data — `get_host_snapshot`, `get_live_network_reading`,
  `get_network_overview` (configured interface + persistent totals/rate + full
  `traffic_analytics` bundle), `get_vps_inventory_overview` (latest synced inventory + fleet
  analytics), `get_renewal_overview` (thin wrapper reusing `menu.collect_dashboard_data`
  unchanged — no second implementation of that aggregation), `get_emergency_policy_overview`,
  and `get_full_snapshot` (bundles all of the above in one call).
  - **Every function is read-only toward every local cache it touches and NONE call the
    Virtualizor API directly** — verified by a dedicated test
    (`test_network_overview_never_calls_virtualizor_api`) that monkeypatches
    `writer.call_vs_api` to raise if ever invoked from this path.
  - Unconfigured/empty states (no interface chosen yet, no inventory synced yet, policy
    disabled) are reported honestly (`configured: False`, empty lists, zero counts) rather
    than fabricated — matching the same discipline already established in `hostnet.py`/
    `traffic_analytics.py`.
  - **Deliberate scope decision:** this does NOT refactor `menu.collect_dashboard_data` or
    any of menu.py's existing render functions — it wraps/reuses them. Fully unifying the
    CLI's own dict shapes with this new API was judged too high a regression risk against
    menu.py's already-large, already-tested surface for this checkpoint; it's listed as
    follow-up work below.
- **security.py** provides: `generate_secure_token`/`constant_time_equals` (for future
  session/CSRF tokens), `hash_password`/`verify_password` (stdlib-only PBKDF2-HMAC-SHA256,
  310,000 iterations — OWASP's 2023 minimum — no bcrypt/argon2 dependency added),
  `is_authorized_telegram_id` (numeric-ID-only, by explicit design — there is no `username`
  parameter at all, and a test asserts this via `inspect.signature`), and a SQLite-backed
  sliding-window rate limiter (`check_rate_limit`/`prune_rate_limit_events`) shared by future
  Web login throttling and Telegram command throttling alike.
- **Tests:** `tests/test_security.py` (23 tests) + `tests/test_control_center_api.py`
  (12 tests).

### 5. Telegram bot: Direct + SOCKS5 transports, command dispatch — DONE (this checkpoint)
*(relay transport: signing/verification protocol done and tested; the separate relay WORKER
process is not — see "Deliberately NOT done" below)*
- **Files:** `implementation/telegram_transport.py`, `implementation/telegram_config.py`,
  `implementation/telegram_bot.py` (all new), zero third-party dependency (pure stdlib
  `urllib`/`socket`/`ssl`, matching `writer.py`'s own existing convention for talking to
  Virtualizor).
- **DirectTransport** (default): real HTTPS to `api.telegram.org`, token redacted from every
  exception message (same discipline as `writer.py`'s credential redaction).
- **Socks5Transport**: a REAL, from-scratch SOCKS5 CONNECT handshake (RFC 1928, with RFC
  1929 username/password sub-negotiation) over a raw socket, then TLS + a minimal hand-built
  HTTP/1.1 request/response — no third-party SOCKS library. **Tested against a real local
  SOCKS5-speaking TCP test server** (not a mock of the handshake) — this caught a real bug
  in the test server itself (a malformed greeting-read that caused a handshake deadlock),
  fixed before any of this touched real code.
- **Relay mode**: `sign_relay_request`/`verify_relay_request` implement an HMAC-signed,
  replay-resistant, timestamp-bounded envelope scheme for the Iran host to hand outgoing
  requests to a separate relay worker and verify what comes back — fully implemented and
  tested (tamper detection, replay rejection via a caller-supplied seen-nonce set, expiry).
  **`telegram_bot.build_transport()` deliberately refuses to construct a direct-network
  transport object for RELAY mode at all** — there is no code path by which relay mode could
  silently fall back to talking to Telegram directly.
- **telegram_config.py**: secrets (bot token, SOCKS5 password, relay shared secret) use
  `credentials.py`'s exact 0600-enforced storage discipline (atomic write, permission check
  on read, fails closed on loose permissions). Settings (admin/viewer IDs, connectivity
  mode, report schedule) default to `enabled=False`, `connectivity_mode=DIRECT` — the
  mission's required defaults — and fall back to those same defaults on any read error.
- **telegram_bot.py**: `process_update()` is the pure, network-independent dispatch core --
  parses a Telegram update dict, enforces rate limiting (`security.check_rate_limit`, per
  numeric user ID) BEFORE any command logic runs, enforces numeric-ID-only authorization
  (`security.is_authorized_telegram_id` — a dedicated test confirms a spoofed/matching
  `username` field can never substitute for a real admin ID), then dispatches to one of 10
  command handlers, ALL built on `control_center_api.py` (never re-deriving business logic).
  `/reports` and `/backup` report themselves as not-yet-implemented rather than fabricating
  a status. Every handler is wrapped so one bad update can never crash the dispatch loop.
- Wired into the CLI as "Configure Telegram bot (optional)" — sets mode/admins/secrets,
  never starts a bot process, offers an explicit opt-in connectivity test (a real `getMe`
  call) before saving anything live.
- **Tests:** `tests/test_telegram_transport.py` (17, including the real local SOCKS5
  server), `tests/test_telegram_config.py` (14), `tests/test_telegram_bot.py` (27) + 1 CLI
  wiring test in `tests/test_menu.py`.

### 6. Telegram long-polling loop — DONE (this checkpoint; recovered from an interrupted session)
- **Functions:** `telegram_bot.run_polling_loop()`, `load_last_update_id()`/
  `save_last_update_id()` (atomic offset persistence so a restart never re-delivers already-
  processed updates), and `renewal_cli.cmd_telegram_bot_run` wired to a new
  `telegram-bot-run` subcommand (`--max-iterations` bounds it for manual smoke-testing; the
  real deployment shape runs forever, matching a `Type=simple, Restart=always` systemd
  service rather than the existing timer-triggered-oneshot units).
- Calls `getUpdates` (long-polling, so the host needs no inbound port), feeds each update to
  the already-tested `process_update()`, sends any reply back via the same transport's
  `sendMessage`, and persists the offset after every non-empty batch.
- **Refuses to start at all** unless `enabled: true` is set (via the menu's "Configure
  Telegram bot" screen) and both settings and secrets load successfully — verified by
  dedicated tests for each blocked case (`test_cmd_telegram_bot_run_blocked_when_disabled`,
  `test_cmd_telegram_bot_run_blocked_when_secrets_missing`).
- A `getUpdates` transport failure is logged and the loop continues (Telegram being briefly
  unreachable must never crash a long-running process); an unexpected non-transport
  exception is left to propagate, since that indicates a real bug worth stopping for.
- **Tests:** 9 new in `tests/test_telegram_bot.py` for the polling loop itself (offset
  resume across restarts, reply delivery, error survival, empty-batch no-op) + 4 for the
  `renewal_cli.py` wiring (argparse registration, both blocked-start cases, a full
  start-and-stop run with a mocked transport).

### 7. Scheduled reports — DONE
- **File:** `implementation/reports.py` (new). `generate_report_text()` builds a full
  text report from `control_center_api.get_full_snapshot()` alone (host health, network
  traffic -- today/week/month, explicitly separating the raw live kernel counter reading
  from persistent recorded totals and never adding one into the other, VPS inventory,
  renewals, emergency-policy queue/grants) — every section says so honestly when there's
  no data yet, and the backup section still says "not yet implemented" (see above). VPS
  restarts are never mentioned at all (no reliable source wired), matching the explicit
  "never infer a restart from current running status" instruction.
- `chunk_telegram_message()` splits a report that exceeds Telegram's 4096-character limit
  on line boundaries (hard-splitting only a single overlong line as a last resort) —
  tested for lossless reconstruction.
- `send_report_to_admins()` sends to every admin independently; a report is marked
  delivered for a given admin **only if Telegram's own API response says `ok: true`** for
  every chunk sent to them — one admin's failure never blocks or falsifies another's
  result, and a transport-level exception is caught and recorded, never raised.
- **No new systemd timer.** Scheduling (`should_send_scheduled_report()`, idempotent once
  per local calendar day in the configured `report_timezone`, never backlogs missed days)
  is checked once per iteration of the ALREADY-RUNNING `telegram_bot.run_polling_loop` —
  gated by a new, separate `TelegramSettings.reports_enabled` flag (default `False`), so
  simply enabling the bot does not silently start pushing daily reports. The report is
  only marked "sent today" if delivery succeeded for every configured admin.
- Wired into the CLI as a `send-report` subcommand (manual, immediate, for testing —
  independent of `reports_enabled`) and into `/reports`' bot command (now shows the real
  configured schedule and last-sent date instead of a placeholder) and `/settings`.
  `menu.py`'s "Configure Telegram bot" screen now also configures the report schedule.
- **Tests:** `tests/test_reports.py` (21) + 8 more in `tests/test_telegram_bot.py` (loop
  integration + `send-report` CLI wiring) + 2 updated `/reports` tests.

### 8. Timezone-aware host uptime, boot time, and monitoring uptime — DONE
- **File:** `implementation/host_time.py` (new) — the one shared implementation for the
  CLI, `control_center_api.py`, the Telegram bot, and (when built) reports/Web.
- **Uptime:** `read_uptime_seconds()` reads `/proc/uptime` directly (the kernel's monotonic
  since-boot counter, immune to clock adjustments) — real smoke-tested against this host's
  actual file, not mocked. `format_duration_human()` always shows days, hours, minutes, AND
  seconds (e.g. `"2d 3h 12m 45s"`), distinct from `menu.py`'s existing shorter
  `_format_uptime_human` (left untouched — that one deliberately omits seconds for the
  compact dashboard view; this is a new, more detailed formatter for the new screens).
- **Boot time:** `compute_boot_time_utc()` = `now - uptime` (the same method `uptime`/
  `who -b` use), explicitly documented as an ESTIMATE, never claimed as a kernel-reported
  absolute value (Linux doesn't expose one). **Boot ID:** reuses `hostnet.get_boot_id()`
  unchanged (no second implementation of that one-line `/proc/sys/kernel/random/boot_id`
  read).
- **Timezone:** `detect_default_display_timezone()` suggests the HOST's own system
  timezone (read-only via `hostinfo.get_host_timezone()`, which was already read-only and
  unchanged) as a default, falling back to the project's existing `Asia/Tehran` default if
  the host timezone is unavailable or not a valid IANA name — the host's actual system
  timezone/clock are never touched, only read for the suggestion.
- **Calendar preference:** a new, additive `calendar_preference` key (`auto`/`jalali`/
  `gregorian`/`both`, default `auto` — zero behavior change for existing installations)
  stored in the SAME `display-settings.json` file `menu.py` already manages (merged in,
  never a second divergent settings file). `format_local_datetime()` honors it — `auto`
  preserves the exact pre-existing "Jalali only for Asia/Tehran" behavior; `jalali`/
  `gregorian` force one calendar regardless of timezone (an explicit administrator
  override); `both` shows both together. Always 24-hour clock time. A real DST-transition
  test (Europe/London's spring-forward) confirms the underlying `zoneinfo` conversion is
  correctly wired through, not broken by this module's thin formatting layer.
- **Monitoring activation/elapsed, last persistent sample, data coverage:** read straight
  from `hostnet.py`'s existing cumulative/sample tables — no new persistence, no new
  polling. **Kernel counter section:** one raw RX/TX read (via the SAME primitive
  `control_center_api.get_live_network_reading` already uses) presented as `combined_bytes`
  plus an explicit, always-present "reset semantics" label stating it is reset-on-reboot
  and NEVER added to the persistent recorded totals — verified by a dedicated test that
  deliberately records a huge fake persistent total and confirms the live kernel reading
  (this host's real, unrelated `lo` counters) is never summed into it.
- **Wired into:** `control_center_api.get_host_time_overview()` (new, included in
  `get_full_snapshot()`); `menu.py`'s "System & Virtualizor information" screen (new
  "SERVER TIME & UPTIME" section, backward-compatible — omitted entirely if no snapshot is
  passed, so every pre-existing call site/test is unaffected) and the hostnet panel (new
  "KERNEL COUNTER" section, same backward-compatible opt-in pattern); `menu.py`'s
  "Configure display timezone" screen (now also suggests the detected host timezone and
  prompts for calendar preference); `telegram_bot.py`'s `/host` (full detail) and `/status`
  (one-line summary) commands.
- No Virtualizor API call anywhere in this feature; no new systemd timer; no change to any
  stored UTC timestamp anywhere in the project — only new, additive display conversions.
- **Tests:** `tests/test_host_time.py` (40 tests) covering uptime formatting (including a
  real `/proc/uptime` read and a real boot-ID read), boot time estimation, calendar
  preference persistence/validation, DST-correct conversion, all four calendar-preference
  modes, and honest missing-data handling (no interface configured, no samples yet, kernel
  counter unavailable) — plus CLI/Telegram wiring smoke tests.

### 9. Telegram bot systemd unit — DONE
- A new `TELEGRAM_BOT_SERVICE_UNIT_TEMPLATE` in `renewal_cli.py`, generated by the same
  `write_systemd_units()` every other unit already goes through (one code path, never a
  second one to drift out of sync) — `virt-renewal-telegram-bot.service`, `Type=simple`/
  `Restart=always` (NOT a `.timer`-triggered oneshot like the other 5, since this holds a
  persistent long-poll connection; there is deliberately no corresponding `.timer` file).
  Generated-but-never-installed/enabled, exactly like every other unit — and the bot itself
  independently refuses to start unless explicitly enabled via the menu, so generating (or
  even installing) this unit alone activates nothing.
- Added to `UNINSTALL_UNIT_NAMES` and `cmd_uninstall`'s printed system-wide cleanup
  instructions — the same "previously-missed-unit-left-behind" regression class this
  project has hit before (see the notifier/inventory-sync history in `cmd_uninstall`'s own
  comments) is guarded against from day one here instead of being a bug to fix later.
- **Tests:** 1 new in `tests/test_renewal_system.py` (confirms `Type=simple`/
  `Restart=always`/no `.timer` file) + 2 existing install/uninstall-completeness tests
  extended to include this unit in their checked list.

### Combined test count
- Pre-existing (as of v1.7.3): 675 passing.
- New this session: 248, across `tests/test_traffic_analytics.py`,
  `tests/test_emergency_policy.py`, `tests/test_security.py`,
  `tests/test_control_center_api.py`, `tests/test_telegram_transport.py`,
  `tests/test_telegram_config.py`, `tests/test_telegram_bot.py`, `tests/test_reports.py`,
  `tests/test_host_time.py`, `tests/test_renewal_system.py`, and several new cases added to
  `tests/test_menu.py` for CLI wiring.
- **Total: 923 passing** (verified by running the full suite), no skips, no mocked-away
  failures.

---

### 10. Web Control Center — DONE (real, functional, all 16 reachable pages)
Stdlib-only (`http.server`, zero third-party dependency). New modules: `web_auth.py`
(sessions/CSRF/users — committed previously), `web_config.py` (settings, disabled/
localhost-only by default), `web_server.py` (`WebApp.handle_request()` is the pure,
socket-free core; `TixoRequestHandler`/`run_server_in_thread` the thin real-socket layer).

- **All 16 pages render with real data**, wired to `control_center_api.py` /
  `emergency_policy.py` / `reports.py` / `host_time.py` / `menu.py` — never fabricated.
  Backups page now creates/lists real archives (see section 11 below).
  Telegram Settings page is read-only (editing stays in the CLI menu, to limit scope).
- **Auth:** session-cookie login, server-side SQLite sessions, login rate limiting
  (reuses `security.py`), RBAC (`admin` vs `viewer`) enforced on every mutating route.
- **CSRF:** every POST (mode changes, review resolve/ack, user management, settings,
  manual report send) requires a per-session derived token — verified by dedicated
  reject/accept tests.
- **Audit:** every mutating action is recorded via `audit_log.py` and visible on `/audit`.
- **Localization/theme:** `en`/`fa` with correct `dir="rtl"` for Farsi, `dark`/`light` via
  CSS variables — both verified by test.
- **Live monitoring:** `/host` polls `/api/live` every 2s via vanilla JS — one raw kernel
  counter read per poll, **zero Virtualizor API calls** (verified by a test that
  monkeypatches `writer.call_vs_api` to raise if ever reached).
- **Real end-to-end test:** one test spins up an actual server on a real ephemeral
  localhost port and makes genuine HTTP requests via `urllib` — not just direct function
  calls.
- Wired into `renewal_cli.py` as `web-run` (blocks; refuses to start unless explicitly
  enabled, same pattern as the Telegram bot) and `menu.py`'s "Configure Web Control
  Center" screen (settings + first-admin-user creation; never starts a server itself).
- **NOT done:** no systemd unit template yet for `web-run` (same category of follow-up as
  the Telegram bot's, which now has one); no HTTPS/certificate handling; no reverse-proxy
  guidance page.
- **Tests:** `tests/test_web_auth.py` (34), `tests/test_web_config.py` (7),
  `tests/test_audit_log.py` (4), `tests/test_web_server.py` (44) = **89 new tests**.

### 11. Secure backup functionality — DONE (archive, retention, GPG encryption, safe restore)
Extends the pre-existing `implementation/backup.py` (per-node `backup_db`/`list_backups`/
`restore_db`, unchanged) rather than duplicating it:

- **`create_full_backup_archive(state_dir, config_dir, backup_root, label=None)`** — bundles
  consistent `Connection.backup()` snapshots of every known state DB (`inventory.sqlite3`,
  `hostnet.sqlite3`, `emergency_policy.sqlite3`, `self.sqlite3`) plus a deliberately
  **ALLOWLISTED** (never blocklisted) set of non-secret config files (`policy.json`,
  `nodes.json`, `display-settings.json`, `hostnet-settings.json`,
  `emergency-policy-settings.json`, `telegram/settings.json`, `web/settings.json`) into one
  `tar.gz`. `credentials/`, `telegram/secrets.json`, `web/users.json`, `web/csrf_secret.key`
  are never read, regardless of future config additions — verified by
  `test_create_full_backup_archive_never_includes_secrets` and the Web-UI-level
  `test_backups_page_never_exposes_secret_files`.
- **`verify_backup_archive(path)`** — tar readability + `PRAGMA integrity_check` on every
  bundled `.sqlite3`; corruption is detected (test deliberately corrupts DB bytes and confirms
  `ok: False`).
- **`apply_retention(backup_root, keep_count)`** — deletes all but the N most recent archives.
- **`encrypt_backup_file`/`decrypt_backup_file`** — symmetric AES256 via the system `gpg`/
  `gpg2` binary (confirmed present on this host), passphrase piped over **stdin**
  (`--passphrase-fd 0`), never a CLI argument `ps` could reveal. Raises
  `BackupEncryptionUnavailable` with a clear message if neither binary exists, rather than
  silently writing an unencrypted file when encryption was requested. No cryptography is
  hand-rolled.
- **`restore_full_backup_archive(archive_path, staging_dir)`** — extracts to a staging
  directory ONLY; verifies integrity first and refuses a failed archive; never overwrites
  live `state_dir`/`config_dir` files itself (that remains a deliberate, separate, manual
  step for an operator).
- **CLI:** `renewal_cli.py backup-full [--label L] [--encrypt] [--keep N]` — encryption
  passphrase must come from the `TIXO_BACKUP_PASSPHRASE` env var (refuses with a clear error
  if unset, rather than silently skipping encryption); records a `backup_full_created` audit
  event via `audit_log.py` every time.
- **Web UI:** `/backups` page replaced its former placeholder — admin+CSRF-gated "Create
  backup now" button (calls the same `create_full_backup_archive`/`verify_backup_archive`),
  lists existing archives, records `backup_created` audit events, and documents that
  encryption/retention/scheduling live in the CLI for now.
- **Tests:** `tests/test_backup_archive.py` (16: archive contents, secret exclusion,
  integrity verification incl. corruption detection, retention, GPG encrypt/decrypt
  roundtrip, wrong-passphrase rejection, `BackupEncryptionUnavailable`, staging-only restore,
  corrupt-archive refusal, and 4 CLI-level tests for `backup-full`) + 4 new tests appended to
  `tests/test_web_server.py` (viewer blocked, admin creates + audited, page lists archives,
  secrets never in the produced tarball) = **20 new tests**.
- **NOT done:** no scheduling (cron/timer) for `backup-full` yet — it is a manual/CLI-only
  trigger today, same category of follow-up as the systemd units already built for the
  Telegram bot and (not yet) the Web server; no Telegram delivery of backup archives yet
  (explicitly listed in the mission but not started this pass — see below).

### 12. Relay worker process — DONE (locally simulated; real cross-host run still pending)
Completes the previously protocol-only relay mode (`sign_relay_request`/
`verify_relay_request` were already done and tested). New: `implementation/relay_worker.py`
-- the actual SEPARATE program meant to run on a VPS that can reach Telegram.

- **Protocol:** the Iran-side host makes an OUTBOUND-only HTTPS POST (never an inbound
  connection) to the relay worker's `/submit` endpoint carrying a signed envelope. The
  worker verifies signature + replay (durable SQLite seen-nonces table, survives restarts) +
  timestamp freshness, executes the requested Telegram Bot API call, signs the result with
  the same shared secret, and returns it synchronously -- no separate poll-for-result
  round trip, and the relay worker never initiates a connection back to the Iran host.
- **`RelayWorkerApp.handle_request()`** is the same pure, socket-free core pattern as
  `web_server.py`'s `WebApp` -- fully testable without a real socket; a thin
  `http.server`-based wrapper (`build_server`/`run_server_in_thread`) is the only part that
  touches one.
- **`telegram_transport.RelayClientTransport`** (Iran-side) is drop-in compatible with
  `DirectTransport`/`Socks5Transport`'s `.call()` signature -- `telegram_bot.build_transport()`
  now actually constructs it for RELAY mode (previously raised unconditionally), verifies
  the worker's signed response before trusting it, and raises rather than silently trusting
  an unverifiable or tampered reply.
- **Secrets:** the relay worker has its own `telegram_config.RelayWorkerSecrets`
  (bot_token + shared_secret) in its OWN config_dir on its OWN host -- it shares no
  filesystem or secret store with the Iran-side host. Both sides store the shared secret
  with the same 0600-enforced discipline as everything else.
- **Disabled by default:** `RelayWorkerSettings.enabled = False`, binds to `127.0.0.1` by
  default; `renewal_cli.py relay-worker-run` refuses to start unless explicitly enabled AND
  secrets are stored, mirroring the Telegram bot/Web Control Center pattern exactly.
- **Menu:** new "Configure Relay Worker (optional, run on a SEPARATE host)" screen (writes
  settings/secrets only, never starts the process); the existing "Configure Telegram bot"
  screen's RELAY branch now collects `relay_port`/`relay_use_tls` and the matching shared
  secret, and no longer prints the old "not yet implemented" note.
- **Local simulation, honestly labeled:** `test_real_server_end_to_end_simulated_second_host`
  spins up a genuine `relay_worker` HTTP server on a real localhost port and drives it
  through `RelayClientTransport` over an actual socket -- exactly the "simulate locally
  since no second host is available" allowance in the mission. **A real two-host
  cross-network validation has NOT been performed** and is explicitly still required before
  this feature can be considered production-validated.
- **Tests:** `tests/test_relay_worker.py` (15: envelope execution, bad signature, replay,
  stale timestamp, malformed body, unknown route, Telegram-call failure surfaced as a
  signed failure rather than an exception, `RelayClientTransport` success/tamper-rejection/
  reported-failure, the real-socket simulated end-to-end test, and 3 CLI wiring tests) + 11
  new tests in `tests/test_telegram_config.py` (relay field roundtrip,
  `RelayWorkerSettings`/`RelayWorkerSecrets` defaults/validation/roundtrip/permissions) + 1
  rewritten test in `tests/test_telegram_bot.py` (relay-mode `build_transport` now
  constructs a real `RelayClientTransport` instead of always raising) + 1 menu-wiring test.

### 13. Remote management architecture — DONE as a read-only MVP (honestly scoped)
Adds a lightweight, hypervisor-side agent so a SEPARATE central management server can see a
host's status remotely without ever being handed that host's Virtualizor API credentials.
Local install mode (this host polling its own Virtualizor node directly) is completely
unaffected and remains the default everywhere -- this is an additional, optional, disabled-
by-default surface, not a replacement.

- **`implementation/agent_server.py`**: `AgentApp.handle_request()` is the same pure,
  socket-free core pattern as `web_server.py`/`relay_worker.py`. Exposes exactly one
  endpoint, `/query`, accepting a signed envelope naming one of an explicit allowlist of
  READ-ONLY queries (`snapshot`, `host`, `network`, `inventory`, `renewals`, `health`) --
  there is no mutating action reachable through this protocol at all (no renewal-apply, no
  emergency-grant action, no credential rotation). Every query is answered by calling
  `control_center_api.py`/`menu.py` directly -- never the Virtualizor API itself (verified
  by `test_agent_never_calls_virtualizor_api`, which monkeypatches `writer.call_vs_api` to
  raise if ever reached).
- **Authentication reuses, rather than duplicates, the relay protocol**: `telegram_transport.
  sign_relay_request`/`verify_relay_request` were already generic HMAC signed-envelope
  primitives (never actually Telegram-specific despite the module name) -- durable replay
  protection (SQLite seen-nonces) and timestamp-freshness checking come for free.
  **Honest limitation:** this provides authenticity/integrity, not transport
  confidentiality -- it speaks plain HTTP, so binding beyond `127.0.0.1` requires an
  operator-provided TLS reverse proxy or VPN, the same documented caveat as
  `web_server.py`/`relay_worker.py`.
- **Secrets/enrollment**: `agent_config.AgentSecrets` (one shared secret, 0600-enforced,
  same discipline as everywhere else) plus `rotate_agent_secret()` -- rotation is an atomic
  overwrite, so a management server holding the old secret is immediately locked out until
  updated, satisfying "secure credential rotation."
- **Disabled by default**: `AgentSettings.enabled = False`, binds to `127.0.0.1`.
  `renewal_cli.py agent-run` refuses to start unless explicitly enabled AND a secret is
  enrolled; a new "Configure Remote Agent (optional, read-only)" menu screen writes
  settings/secrets only, never starts the process.
- **`agent_server.query_agent()`** is the management-server-side client (sign, POST, verify
  the signed response, raise rather than trust an unverifiable/tampered/error reply) --
  validated against a real local HTTP server over a genuine socket
  (`test_real_server_end_to_end_over_a_real_socket`), same "simulate locally, label it
  honestly" approach used for the relay worker.
- **NOT done / explicitly out of scope for this pass**: no actual second "management
  server" program that aggregates multiple agents into one dashboard (today only the
  client-side `query_agent()` helper and a protocol exist; building a real multi-host
  aggregating UI would reuse `web_server.py`'s page patterns but is a separate, sizeable
  piece of work); no mutating actions over this protocol at all (deliberate, matches "least-
  privilege" and "safe read-only defaults"); no real cross-host validation (same caveat as
  the relay worker).
- **Tests:** `tests/test_agent.py` (31: settings/secrets validation+roundtrip+permissions+
  rotation, every allowed query succeeding, disallowed-query rejection, bad signature,
  replay, stale timestamp, malformed body, unknown route, never-calls-Virtualizor-API,
  `query_agent()` success/unknown-query/tamper-rejection/reported-error, the real-socket
  end-to-end test, 3 CLI wiring tests, 1 menu-wiring test).

### 14. Installer coverage for all optional modules — DONE (generation only, same safety discipline)
`write_systemd_units()` (the ALREADY-shared generator used by both `cmd_install` and the
menu, so there is exactly one code path, never two to keep in sync) now also generates
`virt-renewal-web.service`, `virt-renewal-relay-worker.service`, and
`virt-renewal-agent.service` -- the same `Type=simple`/`Restart=always`, generated-but-
never-installed-or-enabled shape already used for `virt-renewal-telegram-bot.service`. Each
process still independently refuses to start unless explicitly enabled via its own menu
screen, so generating these files activates nothing by itself. `cmd_uninstall`'s cleanup
list and printed system-wide removal commands were extended to match (previously would have
silently left these three behind). `deploy/install.sh`'s final output now also names all
four optional modules and exactly which menu screen enables each one, rather than only ever
mentioning the core monitor timer.
- **Tests:** extended `tests/test_renewal_system.py`'s existing install/uninstall
  completeness regression tests to cover the three new units, plus a new parametrized
  `test_optional_persistent_units_are_simple_restart_always_not_timers` (3 cases).
- **NOT done:** this is unit-file GENERATION coverage only, not the full "unified
  installer" the mission describes -- no interactive/noninteractive mode selection
  (local/remote), no domain/subdomain or HTTPS/cert setup, no timezone/Jalali-calendar
  selection at install time (that remains a post-install menu action), no repair/upgrade-
  in-place logic beyond what the already-idempotent `cmd_install`/`write_default_config`
  provide. See "Exact next steps" below for the real remaining scope.

### 15-16. Backup scheduling, encryption-gated Telegram delivery — DONE
- **`backup_config.py`** (new): `BackupScheduleSettings` (disabled by default;
  `interval_hours`/`retention_keep`/`encrypt`/`telegram_delivery_enabled` --
  `telegram_delivery_enabled=True` is REJECTED unless `encrypt=True`, enforced in
  `__post_init__`, never just a convention). The unattended timer's GPG passphrase is
  stored at rest with the same 0600-enforced discipline as every other secret in this
  project -- an explicit, documented tradeoff (root-equivalent access either way).
- **`telegram_transport.py`**: `DirectTransport.send_document()` -- a hand-built
  multipart/form-data encoder (stdlib has no multipart support) that checks the file size
  BEFORE reading it into memory and refuses anything over `max_size_bytes` (Telegram's own
  real 50MB bot-upload limit by default). `Socks5Transport`/`RelayClientTransport` raise a
  clear `NotImplementedError` for `send_document` rather than silently mis-delivering --
  multipart over the from-scratch SOCKS5 raw-HTTP path and embedding binary in a
  HMAC-signed relay envelope were both judged not worth the added risk surface.
- **`backup.py`**: `send_backup_to_telegram()` refuses a non-`.gpg` archive by default
  (`require_encrypted=True`), enforces the size cap, and refuses outright if the transport
  doesn't support `send_document` at all -- one admin's delivery failure never blocks
  delivery to the others.
- **`renewal_cli.py backup-scheduled-run`**: the timer's actual entry point -- reads
  `BackupScheduleSettings`/the stored passphrase (never an env var, since a timer has no
  interactive terminal), refuses to run unless explicitly enabled, records every outcome
  (including a skipped/blocked Telegram delivery) to the audit log.
- **`write_backup_systemd_unit()`**: deliberately a SEPARATE function from the shared
  `write_systemd_units()` so reconfiguring the backup interval can never reset another
  timer's already-customized interval -- a regression class this project has hit before
  with shared generator functions.
- **Menu:** "Configure Backup Schedule" screen.
- **Tests:** `tests/test_backup_config.py` (11) + 17 new in `tests/test_backup_archive.py`
  (multipart send/size-limit/transport-capability/multi-admin-failure-isolation,
  `backup-scheduled-run` blocked/unencrypted/encrypted/audit/delivery-skip cases) + 7 in
  `tests/test_telegram_transport.py`.

### 17. Unified installer core — DONE (detect/upgrade/repair/rollback/health-check)
**`installer.py`** (new) -- shared by both the interactive menu wizard and noninteractive
CLI, so there is exactly one implementation of each operation:
- `detect_installation_state()` -- read-only; fresh vs. existing (via `nodes.json`/
  `policy.json`/any `.sqlite3` presence).
- `check_port_available()` -- a real bind-and-close probe, never a guess.
- `validate_https_prerequisites()` -- cert/key existence, THAT THEY LOAD TOGETHER (catches
  a mismatched pair before a confusing runtime failure), and port-free check. No network
  calls (no DNS, no CA validation) -- a local readiness check, not a trust assertion.
- `request_automatic_certificate()` -- checks certbot presence + port 80 availability;
  **never invoked automatically by anything in this codebase** -- only attempts issuance
  (via an injectable `run_fn`, so tests never shell out for real) when a caller explicitly
  calls it after an operator's explicit confirmation. Never opens a firewall port itself --
  only checks whether port 80 is already free.
- `backup_before_change()`/`rollback()` -- every upgrade/repair backs up FIRST; rollback
  integrity-verifies the source archive, safety-copies the CURRENT state before overwriting
  anything (mirrors `backup.restore_db`'s existing `.pre-restore` discipline), and reports
  exactly what was restored.
- `upgrade()`/`repair()` -- idempotent and config-preserving: re-runs the same
  directory/default-config/schema/systemd-unit generation `cmd_install` already does (all
  of it already idempotent), NEVER overwrites an existing `nodes.json`/`policy.json`.
  `repair()` additionally re-tightens any 0600 secret file whose permissions were loosened
  (chmod only ever tightens, never widens).
- `health_check()` -- read-only post-change sanity check (nodes/policy/optional-module
  settings all load without error); never restarts or enables anything.
- **CLI:** `install-detect`/`install-upgrade`/`install-repair`/`install-rollback
  --backup-file F --yes`/`install-health-check`.
- **Menu:** "Install / Initial Setup" now detects fresh vs. existing and offers
  upgrade/repair (with an automatic post-change health check) instead of blindly re-running
  a fresh install every time; the Web Control Center config screen gained domain/HTTPS
  prompts wired to `validate_https_prerequisites()` and a port-conflict warning.
- **`web_server.py`**: `build_server()`/`run_server_in_thread()` now wrap the listening
  socket with a REAL `ssl.SSLContext` when both `tls_cert_path`/`tls_key_path` are given
  (manual certificate paths only -- this module never requests a certificate itself).
  Raises `TlsCertificateError` (never silently falls back to plain HTTP) on a bad/missing
  cert pair. Verified by a real end-to-end test over an actual TLS socket with a
  self-signed test certificate.
- **Tests:** `tests/test_installer.py` (29) + 5 new in `tests/test_web_server.py` (TLS
  wrap/reject-cert-without-key/reject-unreadable-cert/real-TLS-socket-end-to-end/
  `cmd_web_run`-blocked-on-bad-cert).

### 18. Safe per-module uninstall — DONE
`installer.uninstall_optional_module()` disables exactly ONE optional module
(telegram/web/relay_worker/agent/backup_schedule) -- sets its `enabled=False`, optionally
purges its stored secret ONLY if `--purge-secrets` is explicitly passed (kept by default so
re-enabling doesn't require re-entering credentials), and removes that module's generated-
but-never-installed systemd unit file(s). Verified to never touch the core renewal
install, any other module, or historical data. Wired as `renewal_cli.py uninstall-module
--module X [--purge-secrets]`. **Tests:** 9 new in `tests/test_installer.py`.

### Combined test count
- **Total: 1170 passing** (verified by running the full suite), up from 1091 after the
  installer systemd-unit-generation milestone, 1088 after the remote agent milestone, 1057
  after the relay worker milestone, 1032 after the backup milestone, 1012 after the Web
  Control Center milestone, and 923 at the start of the prior sprint.

---

## Deliberately NOT done in this checkpoint (and why)

These are honestly reported as **not implemented**, not "mostly done" or "mocked":

- **A real cross-host validation of the relay worker AND the remote agent.** Both exist and
  are fully tested locally, but each has only been run against a SIMULATED second host on
  localhost -- no real second VPS was available this session. **This is simulated, not
  real two-server validation, and must not be represented otherwise.** This remains the
  single most important remaining validation step before either feature is production-ready.
- **A real multi-host "management server" program.** The remote agent's protocol and
  management-server-side client (`agent_server.query_agent()`) exist and are tested, but
  there is no actual aggregating dashboard/program that polls multiple enrolled agents.
- **Automatic certificate provisioning has never actually been run.**
  `request_automatic_certificate()` is implemented and unit-tested with an injected
  `run_fn` (so no real `certbot` subprocess has ever executed in this project's history),
  since there is no real public domain/DNS record available to test against safely. The
  manual-certificate-path flow (`validate_https_prerequisites()` + TLS wrapping in
  `web_server.py`) IS exercised against a real, if self-signed, certificate and a real TLS
  socket.
- **No interactive confirmation gate wired between the installer wizard and
  `request_automatic_certificate()` yet** -- the function exists and is safe to call
  (it never acts without an explicit call), but the menu's Web config screen only offers
  the manual-certificate-path flow today; wiring an "attempt automatic issuance" option
  into that same screen is the most natural next step for this feature.
- **No domain/subdomain or HTTPS configuration exists for the Telegram bot, relay worker,
  or remote agent** -- only the Web Control Center gained this. The relay worker and
  remote agent both still speak plain HTTP (HMAC-authenticated, not confidential) and
  document that an operator must front them with a reverse proxy/VPN for real transport
  encryption if exposed beyond localhost.
- **No reverse-proxy guidance page/document.** Operators choosing a reverse proxy in front
  of the Web Control Center (instead of this module's own TLS wrapping) are not given
  worked nginx/Caddy examples anywhere in this repository yet.

---

## Why the version wasn't bumped

`renewal_cli.VERSION` and `deploy/bootstrap.sh`'s default `TAG` remain **v1.7.3** (the last
production-deployed version). This checkpoint is genuinely partial progress toward a much
larger "v1.8.0" milestone described in the mission — bumping the version number now would
misrepresent how much of that milestone is actually done. Per the mission's own instructions
("Do not tag, publish a release, or deploy to production... until the operator reviews the
finished work"), the version bump is deferred to whenever the operator decides a coherent,
reviewable slice (possibly just these two features) is ready to ship as its own release.

---

## Exact next steps (in priority order, matching the mission's own ordering)

1. ~~Add a systemd `.service` unit template for `telegram-bot-run`...~~ **DONE** (see
   section 9 above).
2. ~~Extend `renewal_cli.py`'s `dry-run`/`apply-due` commands...~~ **DONE**: added a
   `check-exhausted` subcommand (`renewal_cli.cmd_check_exhausted`), sharing
   `emergency_policy.check_fleet_exhaustion()` with the menu's interactive screen (the SAME
   implementation, not a parallel one). Still not wired to any systemd timer cadence (no
   new/modified timer this session) and the emergency-grant *auto-apply* step still
   requires the separate `EMERGENCY_GRANTS_APPROVED` marker and is only reachable
   interactively from the menu, never from a noninteractive command yet.
3. ~~Reporting/alerts scheduling...~~ **DONE** (see section 7 above: `reports.py`,
   checked per polling-loop iteration, no new timer).
4. ~~Web dashboard (all pages, auth, CSRF, RBAC, audit, live monitoring)...~~ **DONE**
   (see section 10 above).
5. ~~Secure backup functionality (archive, retention, encryption, verified restore,
   scheduling, encrypted Telegram delivery)...~~ **DONE** (see sections 11 and 15-16
   above). Nothing left open in this item's original scope.
6. ~~The relay worker process...~~ **DONE, locally simulated** (see section 12 above).
   **Still open:** a real end-to-end two-host validation (needs a second host) has not been
   performed. This remains the single highest-priority remaining validation gap, shared
   with item 7 below.
7. ~~Remote management/agent mode...~~ **DONE as a read-only MVP, locally simulated** (see
   section 13 above). **Still open:** a real cross-host validation, and a real multi-host
   aggregating management-server program (only the protocol + client helper exist so far).
8. ~~Unified installer (detect/upgrade/repair/rollback/health-check, domain/HTTPS with
   manual cert paths and real TLS, per-module uninstall)...~~ **DONE** (see sections 14,
   17, and 18 above). **Still genuinely open:** automatic certificate provisioning exists
   but has never actually been exercised against a real domain (only unit-tested via
   dependency injection); no interactive confirmation gate wires it into the menu yet; no
   domain/HTTPS exists for anything other than the Web Control Center.

## Known limitations of what WAS built

- `traffic_analytics.peak_combined_rate_bps` is bounded by the persistent sampler's own
  5-minute granularity — it is the highest rate *the existing sampler observed*, not a true
  per-second peak (that would require either a much finer persistent sampling interval,
  explicitly out of scope per the mission's "preserve the existing five-minute persistent
  hostnet sampler unless explicitly approved" instruction, or deriving peaks from the live
  ~1Hz view, which is intentionally never persisted).
- `emergency_policy`'s cooldown/max-per-month are both simple, independent gates — there is
  no cross-VPS fleet-wide rate limit (e.g. "no more than N emergency grants per day across
  the whole fleet"). Not requested by the mission explicitly, but worth considering before
  enabling this in production with many VPS.
- No migration path was needed for either feature (both use their own new SQLite files), so
  there is nothing to test for upgrade-from-v1.7.3 compatibility beyond the standard
  `CREATE TABLE IF NOT EXISTS` idempotency already exercised by the test suite.

---

## Release-readiness checklist (as of this checkpoint)

### Fully implemented and tested
- Core renewal engine, multi-node support, policy/carryover, emergency-grant policy with
  manual-review queue, inventory sync, host network accounting, traffic analytics,
  Jalali/Gregorian calendar + timezone display (v1.7.3 baseline + prior sprints).
- Telegram bot: DIRECT and SOCKS5 connectivity modes, command dispatch, authorization,
  rate limiting, scheduled reports.
- Web Control Center: all 16 pages with real data, session auth, CSRF, RBAC, audit
  logging, localization (en/fa, RTL), dark/light theme, live host monitoring with zero
  extra Virtualizor API load, real TLS (manual certificate paths).
- Backup: full-archive creation (all known databases + allowlisted non-secret config,
  secrets provably excluded), integrity verification, retention, GPG encryption,
  staging-only verified restore, scheduling via an optional systemd timer, encrypted-only
  Telegram delivery with a size cap and per-admin failure isolation.
- Unified installer core: fresh-install detection, config-preserving upgrade, repair
  (including re-tightening loosened secret permissions), explicit rollback with a
  pre-rollback safety copy, read-only post-change health check, per-module safe uninstall.
- Relay worker protocol and remote-agent protocol: both fully implemented and tested
  against a real local HTTP server over a genuine socket.

### Partially implemented
- **RELAY connectivity mode and the remote management agent**: both complete
  implementations, but validated ONLY against a simulated second host on localhost in
  this environment -- **never against two genuinely separate machines**. Treat as
  "implementation complete, cross-host-validation pending," not "production-ready."
- **Automatic HTTPS certificate provisioning** (`request_automatic_certificate()`):
  implemented and unit-tested via dependency injection; the real `certbot` subprocess path
  has never actually executed, since no public domain was available to test against. Not
  wired into any interactive confirmation flow yet.
- **Document/file delivery over Telegram**: works for DIRECT connectivity mode only.
  SOCKS5 and RELAY modes explicitly raise `NotImplementedError` rather than attempting it.

### Untested real-world scenarios (explicitly, not silently, unverified)
- A genuine two-host relay-worker or remote-agent deployment over a real network path
  (including any real-world latency, intermittent connectivity, or firewall/NAT behavior).
- Automatic certificate issuance against a real, publicly-resolvable domain.
- The Web Control Center, Telegram bot, relay worker, and remote agent all running
  simultaneously under real production load/traffic for an extended period.
- Upgrading a REAL v1.7.3 production installation using `install-upgrade` (the function is
  tested against synthetic fixtures that mimic the v1.7.3 layout, not an actual v1.7.3
  install captured from production).
- Backup restoration of a multi-gigabyte real production database set (tested only with
  small synthetic databases).

### Security assumptions
- An operator with root/filesystem access to this host has equivalent access to every
  secret this software stores (Virtualizor API credentials, Telegram bot token, Web user
  password hashes, relay/agent shared secrets, backup passphrase) -- 0600 permissions
  protect against OTHER unprivileged local users, not against the host's own root.
- HMAC-signed envelopes (relay worker, remote agent) provide authenticity and integrity,
  NOT transport confidentiality -- both speak plain HTTP by default and require an
  operator-provided TLS reverse proxy or VPN for confidentiality if ever exposed beyond
  127.0.0.1.
- The Web Control Center's own TLS wrapping is sufficient for confidentiality+authenticity
  together (manual certificate paths only; no automatic provisioning has been exercised).
- GPG symmetric encryption's security is entirely dependent on the strength/secrecy of the
  chosen passphrase -- this software never judges passphrase strength.

### Installation prerequisites
- Python 3 (stdlib only -- no pip/venv/third-party packages anywhere in this project).
- `systemd` (for every optional/periodic unit; none are installed/enabled automatically).
- `gpg` or `gpg2` (only required if backup encryption is enabled).
- `openssl` (only required to generate/inspect a manual TLS certificate; this software
  never generates a certificate itself).
- `certbot` (only required if automatic certificate provisioning is ever actually
  attempted -- not required for any other feature).
- Outbound HTTPS access to `api.telegram.org` (DIRECT mode) or a reachable SOCKS5
  proxy/relay worker (SOCKS5/RELAY modes) -- only if the Telegram bot is enabled.

### Rollback procedures
1. `renewal_cli.py install-rollback --backup-file <archive> --yes` restores config/state
   from any full backup archive created by `backup-full`/`backup-scheduled-run`/
   `installer.backup_before_change()` (which `install-upgrade`/`install-repair` both call
   automatically before making any change). The CURRENT state is itself safety-copied
   first, so a bad rollback choice is never unrecoverable.
2. To disable (not uninstall) a single optional module without affecting anything else:
   `renewal_cli.py uninstall-module --module <name> [--purge-secrets]`.
3. To fully remove the local install: `renewal_cli.py uninstall [--purge]` (existing,
   pre-sprint command) -- removes locally-generated systemd unit files and, with `--purge`,
   local data; never touches anything installed system-wide (that remains a separate,
   manual, printed `systemctl`/`rm` step).

### Production deployment risks
- None of this session's new features have been exercised against real production
  Virtualizor data, a real second host, or real internet-facing traffic.
- Enabling the Web Control Center, Telegram bot, relay worker, or remote agent on a
  publicly-reachable interface without a TLS/reverse-proxy/VPN layer in front would expose
  either plaintext HTTP (relay/agent) or this project's own (untested-at-scale) TLS
  implementation (Web Control Center) directly to the internet.
- The backup passphrase, if encryption is enabled for the scheduled timer, is stored at
  rest on the host -- anyone who compromises root on this host can decrypt all backups.

### Exact operator actions required before any deployment beyond this dev/test environment
1. Review this checklist and `docs/DEVELOPMENT_PROGRESS.md` in full.
2. Run the full test suite one more time in the target deployment's actual Python version.
3. For RELAY mode or the remote agent: provision a real second host and perform a genuine
   cross-host test BEFORE relying on either feature for anything real.
4. For HTTPS: either supply real, operator-obtained certificate files (manual path,
   already tested) or explicitly trigger `request_automatic_certificate()` against a real
   domain in a controlled test first.
5. Choose and configure a TLS reverse proxy or VPN for the relay worker/remote agent if
   either must be reached from outside 127.0.0.1.
6. Explicitly enable only the optional modules actually needed, via their respective menu
   "Configure ..." screens -- nothing optional activates merely by existing in code.
7. Only after the above: consider installing/enabling the generated systemd units into
   `/etc/systemd/system` (a manual, printed, never-automatic step for every unit in this
   project) and bumping `renewal_cli.VERSION`/tagging a release -- neither was done this
   session, per the explicit "do not publish a release" instruction.

### Release readiness assessment
**Not yet ready for production deployment.** Ready for a **controlled staging/dev
deployment** to begin real-world validation of: RELAY mode and the remote agent across
two actual hosts, the Web Control Center's TLS path with a real certificate, and
`install-upgrade`/`install-repair` against a real (non-synthetic) existing installation.
All locally-testable functionality (1170/1170 tests) passes; the remaining gap is
exclusively real-environment validation that a single-host dev session cannot perform.

---

## Final QA / Release Candidate pass (v1.8.0-rc1)

A dedicated final-QA session re-verified the above, added real end-to-end staging
acceptance tests against the ACTUAL built package (not just in-process unit tests),
performed a real security review (not just "no third-party deps / no shell=True"), and
fixed three real issues found during that review:

1. **Session cookie never marked `Secure` under TLS** -- fixed: `WebApp.tls_enabled`
   (set by `build_server()` whenever a real cert/key pair is configured) now marks the
   login cookie `Secure`.
2. **Unbounded request-body reads** in `web_server.py`/`relay_worker.py`/`agent_server.py`
   -- all three trusted the client-supplied `Content-Length` header without any cap before
   reading that many bytes into memory, a real resource-consumption risk on a busy
   hypervisor. Fixed: all three now cap body size (2MB web, 1MB relay/agent) and return
   413 before reading past the cap.
3. **Non-reproducible release package** -- `build_package.sh` produced a different SHA256
   on every run (tar/gzip both embed timestamps). Fixed: `--sort=name --mtime=@0
   --owner=0 --group=0 --numeric-owner` + `gzip -n` now produce a byte-identical archive
   across builds, verified by a new regression test.

New real end-to-end staging acceptance tests (genuine subprocesses against the actual
extracted `tixo-renewal-package.tar.gz`, not in-process imports):
- Full install -> install-detect -> status -> dry-run -> install-upgrade ->
  install-health-check -> re-install cycle.
- The Web Control Center's real `web-run` CLI command, driven with genuine HTTP requests
  over a real loopback socket: login, all 16 top-level pages returning 200, fa/RTL and
  light-theme cookies, and the live-network endpoint's honest "not configured" response.
- Every optional-module `*-run` command's real `--help` registration and
  blocked-while-disabled behavior.

**Full suite: 1180 passing, 0 failed, 0 skipped** (verified by re-running from a clean
checkout at the start of this QA session, and again after every fix above).

**New documentation** (all in `docs/`): `INSTALL_GUIDE.md`, `WEB_AND_TELEGRAM_SETUP.md`,
`BACKUP_AND_RESTORE.md`, `REMOTE_AND_RELAY.md` (explicit "simulated, not real two-host
validation" limitations), `SECURITY_CHECKLIST.md`, and
`RELEASE_NOTES_v1.8.0-rc1.md` (release notes, staging acceptance results table, upgrade/
rollback instructions, known limitations, and the production deployment checklist).

**Release packaging**: `deploy/build_package.sh` now builds a reproducible
`tixo-renewal-package.tar.gz`, verified to contain no secrets/credentials/database files.
`renewal_cli.VERSION` deliberately remains `1.7.3` -- this release candidate is
**local and unpublished**; no tag was pushed, no GitHub Release was created, matching the
explicit "do not publish" instruction every session in this engagement has operated under.

**Production readiness: BLOCKED on real-infrastructure validation, not on anything fixable
in this environment.** Every locally-verifiable gate passes (tests, security review,
reproducible packaging, rollback, config preservation). The remaining gates --
real cross-host relay/agent validation and real certificate issuance -- require
infrastructure this single-host development environment does not have.

---

## Final completion pass: three-mode backup delivery, UI polish, stable v1.8.0

Closed the remaining genuine gaps identified for final release:

- **Encrypted Telegram backup delivery now works over all three connectivity modes**
  (previously DIRECT-only; SOCKS5/RELAY raised `NotImplementedError`). DIRECT and SOCKS5
  share one real streaming multipart-upload implementation
  (`_stream_send_document_over_socket` in `telegram_transport.py`) -- the file is read in
  bounded 64KB chunks, never loaded into memory at once; SOCKS5's full real protocol path
  (CONNECT handshake -> TLS handshake -> streamed upload) is now actually exercised
  against a real local SOCKS5+TLS test server, closing a pre-existing untested-happy-path
  gap. RELAY mode gained a real document-delivery path through a new, independently-
  capped `relay_worker.py` `/submit-document` endpoint (base64 envelope, never the
  passphrase or any secret, replay-protected exactly like `/submit`).
  `backup.send_backup_to_telegram()` gained bounded per-admin retries (never retrying a
  permanent `NotImplementedError`) and a dedupe marker so re-running delivery for the same
  archive never re-sends to an admin who already received it.
- **Network interface display**: added `hostinfo.classify_interface_category()`
  (physical/bridge/virtual) and `filter_interfaces()` (category + name search) on top of
  the existing compact/advanced split -- the advanced menu screen now prompts for an
  optional filter. Confirmed never affects `hostnet_sync.py`'s own interface selection.
- **VPS inventory table**: `render_vps_table()` now adapts across three terminal-width
  tiers (wide/medium/narrow) instead of two, with display-width-aware ellipsis truncation
  (East Asian wide characters counted as two columns) and an always-visible distinction
  between unlimited-quota and genuinely-zero-usage VPS.
- **Security review fixes carried from the prior QA pass, re-verified**: `Secure` session
  cookie under TLS, bounded request bodies on every HTTP server.
- **Stable version bump**: `renewal_cli.VERSION` 1.7.3 -> **1.8.0** (not an RC label --
  the actual stable version this release represents). `deploy/bootstrap.sh`'s default
  pinned tag updated to match; `deploy/install.sh` now prints the installed version; the
  Web Control Center gained a version footer (previously had none).

**Full suite: 1221 passing, 0 failed, 0 skipped.** Release package rebuilt from this
final source, reproducible (byte-identical across two builds), verified to contain no
secrets/credentials/databases.

**SHA256 (stable v1.8.0 package): `def4207414cea11c64f979af7691554244206c786d671c99f636ebdc0e63fefc`**

New doc: `RELEASE_NOTES_v1.8.0.md` (supersedes the RC notes, kept for history).

**This version bump, rebuild, and local packaging are development/packaging actions, NOT
a publish action** -- no git tag was created or pushed, no GitHub Release was created.
Publishing the release and deploying to `ir1.tixocloud.com` each remain gated behind a
separate, explicit operator approval (see the Final Approval Report for this session).
