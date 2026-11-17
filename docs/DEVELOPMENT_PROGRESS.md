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
  Backups page honestly says "not yet implemented" (true — no backup module exists yet).
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

### Combined test count
- **Total: 1012 passing** (verified by running the full suite), up from 923 at the start
  of this sprint (675 pre-existing + 89 web + prior sprint's 248 — some net additions from
  wiring tests along the way).

---

## Deliberately NOT done in this checkpoint (and why)

These are honestly reported as **not implemented**, not "mostly done" or "mocked":

- **The relay worker process.** The signing/verification *protocol* (`sign_relay_request`/
  `verify_relay_request`) is done and tested; the actual second program meant to run on an
  external VPS (receiving signed envelopes, calling Telegram, signing results back) does not
  exist, and there is no second host available this session to validate an end-to-end
  two-host integration even if it did.
- **Encrypted/scheduled backup delivery via Telegram.** The `/backup` command still
  honestly reports "not yet implemented" — this is genuinely not built (no backup content
  generation, no encryption, no delivery). Scheduled REPORTS, by contrast, ARE now done
  (see section 7 below) — these were previously bundled together in this checkpoint's
  wording but are different features at different states of completion.
- **Web dashboard (backend + frontend, all 17 pages, SSE/WebSocket live updates, auth/RBAC,
  TLS).** Not started.
- **Remote management/agent mode.** Not started.
- **Unified installer (interactive + noninteractive, module install/upgrade/repair/
  reconfigure/disable/uninstall for Web/Telegram/remote-agent).** Not started — the existing
  `deploy/install.sh`/`deploy/bootstrap.sh` continue to work exactly as before (regression-
  tested at 730/730); no new installer surface was added since there is nothing optional yet
  to install.
- **Encrypted off-host backups, audit logging for admin actions beyond what already exists,
  rate limiting, TLS/cert handling.** Not started (these are mostly Web/Telegram-adjacent).

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
   checked per polling-loop iteration, no new timer). **Backup delivery is still not
   done** — no backup content generation, no encryption, no delivery mechanism at all.
   This is the current next step.
4. The relay worker process + an end-to-end two-host validation (needs a second host).
5. Web dashboard backend (reusing `control_center_api.py` + `security.py` directly), then
   frontend.
6. Local + remote install modes, then the unified installer.

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
