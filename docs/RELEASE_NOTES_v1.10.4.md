# TixoCloud Control Center v1.10.4 — Duplicate-Renewal Race Fix & Zero-Default Gift

`renewal_cli.VERSION = "1.10.4"`. A correctness release fixing two real, reproduced
concurrency bugs in the production write path, and correcting the default monthly gift to
match the actual business model (carry-over of purchased bandwidth, not a recurring bonus
grant).

## What's in this release (since v1.10.3)

### A. Fixed a real duplicate-renewal bug (writer.apply_renewal)

`apply_renewal()`'s dedup check (`find_any_prior_operation`, a plain SELECT) and its claim
(`record_operation_start`, a separate INSERT) were not atomic. Two genuinely concurrent
processes — e.g. a manually-invoked `apply-due` overlapping with the systemd-timer-triggered
one, which bypasses the unit's own `flock` wrapper entirely since that lock is applied only
in the generated `ExecStart` line, never inside the Python code itself — could both pass the
SELECT before either INSERTed, then both successfully insert a row for the identical
`(node_id, vpsid, period_key)` and both proceed to write to Virtualizor for the **same**
renewal period.

**Reproduced directly** before any fix existed: two separate `sqlite3` connections to the
same database file (a faithful simulation of two OS processes, since SQLite's locking is
file-based, not connection-based), executing the exact two statements in the exact racing
order — both inserts succeeded, leaving two rows for one period.

Fixed with a `UNIQUE INDEX` on `operations(node_id, vpsid, period_key)`, created
defensively in `ensure_operations_schema()` (never crashes if an existing database already
has pre-fix duplicate rows — prints a warning instead, since that is now a genuine
data-integrity issue requiring manual `resolve-operation` review, not something safe to
paper over). `apply_renewal()` catches the resulting `sqlite3.IntegrityError` and converts
it into the same clean `BLOCKED_UNRESOLVED_PRIOR_OPERATION` result as any other
already-claimed case — never an uncaught exception, never a second live API call.

### B. Fixed the matching race in writer.retry_deferred_renewal

Same root cause, same fix pattern: the deferred-retry claim is now an atomic
`UPDATE ... WHERE status='DEFERRED_LOCKED'` that also flips status away from
`DEFERRED_LOCKED` — only one of two racing UPDATEs can ever match.

### C. Default monthly renewal gift is now 0 GB, not 100 GB

`compute_renewal_quota()` already implements `remaining + monthly_grant_gb` — the business
model is carry-over of **purchased, unused** bandwidth into the next period, not a recurring
bonus. A nonzero `BUILTIN_DEFAULT_MONTHLY_GB` silently handed every fresh install a monthly
grant nobody asked for. The gift feature remains fully supported and configurable (install-
time env var, `policy.json` edit, or the menu's "Configure monthly gift" screen) — just
opt-in rather than on by default. `write_default_policy_config()` is idempotent and never
touches an already-configured `policy.json`, so this cannot retroactively alter any existing
customer's balance.

## New regression tests

- `test_writer.py`: `test_concurrent_claim_race_is_blocked_by_the_database_itself` and
  `test_apply_renewal_converts_a_lost_claim_race_into_a_clean_blocked_result` (bug A),
  `test_concurrent_retry_deferred_renewal_claim_is_atomic` and
  `test_retry_deferred_renewal_sequential_second_call_is_a_clean_no_op` (bug B),
  `test_schema_migration_survives_pre_existing_duplicate_period_key_rows` (defensive
  migration never crashes the whole application).
- Three existing tests whose expected quota math (100 remaining + 100 gift = 200) depended
  on the old default were updated to the correct 100 remaining + 0 gift = 100.

## Scope note

This release does not include any change to the CLI/menu/systemd/installer surface from
v1.10.3 — it is purely a correctness fix to the renewal write path and the shipped default
policy. See `docs/KNOWN_LIMITATIONS.md` for the full, honest scope statement of what has and
has not been independently re-verified in this round (a direct SSH-based investigation of
the real `ir1`/`ir2` production environments was attempted and found to be blocked by a
network-level connectivity limit in the engineering environment, not by missing
credentials — see the closeout report for detail, including an unresolved SSH host-key
change on `ir1.tixocloud.com` that requires separate, out-of-band operator verification
before any future connection attempt).

## Testing

Full regression suite: **1386 passed, 0 failed**.

## Deployment status

Not deployed to `ir1` or `ir2`. No production investigation was possible this round (see
above). This release has not been applied to any live environment.
