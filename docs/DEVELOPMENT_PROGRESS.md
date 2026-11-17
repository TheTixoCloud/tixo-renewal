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

### Combined test count
- Pre-existing (as of v1.7.3): 675 passing.
- New this session: 66 (22 traffic_analytics + 7 menu wiring across both features + 37
  emergency_policy, including the 13 apply-wiring tests above).
- **Total: 741 passing**, full suite, no skips, no mocked-away failures.

---

## Deliberately NOT done in this checkpoint (and why)

These are honestly reported as **not implemented**, not "mostly done" or "mocked":

- **Telegram bot (all 3 connectivity modes).** Not started. This is a substantial, separate
  subsystem (bot command handling, direct/SOCKS5/relay transport, multi-admin RBAC, scheduled
  reports, encrypted backup delivery) — genuinely weeks of work, not something safely
  approximated in this session. No stub, no placeholder menu entry was added, to avoid
  implying partial completion.
- **Web dashboard (backend + frontend, all 17 pages, SSE/WebSocket live updates, auth/RBAC,
  TLS).** Not started, for the same reason.
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

1. Extend `renewal_cli.py`'s `dry-run`/`apply-due` commands (or add a parallel
   `check-exhausted`/`apply-emergency-grants` pair) so the exhausted-bandwidth check and
   (approved) auto-apply can run on the existing systemd timer cadence, not just
   interactively from the menu. Not yet done.
2. Build the shared, secure backend/config service layer (`control_center_api.py` or
   similar) that wraps `traffic_analytics`, `analytics`, `emergency_policy`, `inventory`,
   `hostinfo` behind one stable interface, so CLI/Telegram/Web never reimplement business
   logic independently. **This is the current next step.**
3. Telegram bot: start with Direct mode only, `/status` and `/traffic` commands, numeric-ID
   admin authorization, before attempting SOCKS5/relay transports.
4. Reporting/alerts/backup scheduling.
5. Web dashboard backend (reusing the service layer from step 2), then frontend.
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
