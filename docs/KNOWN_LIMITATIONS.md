# Known Limitations & Technical Debt

Honest list, compiled at project closeout (v1.10.0 + the in-place SOCKS5 fix). Each item
notes impact and suggested next step.

## 1. `deploy/install.sh`'s `APP_VERSION` detection line was broken for several releases

Fixed in dev (commit `4c5569b`) but **not yet in any tagged release** — `install.sh` itself
has never been re-released since the fix. Every `install.sh` run on a pre-existing install
will print a `ModuleNotFoundError: No module named 'renewal_cli'` traceback right after
copying the new code (the copy itself still succeeds) and exit 1 before reaching the
idempotent config step — you then need to run `install-upgrade` directly to finish. Also
fixed in the same commit: `install.sh` never copied the new `lib/static/` directory
(branding assets, `live-bandwidth.js`) introduced in v1.10.0 — this was applied manually to
production (file-for-file, checksum-verified) during that deployment. **Next step:** cut
this fix into the next tagged release so a clean `install.sh` run works end-to-end again.

## 2. Production code currently differs from the last tagged GitHub release by one file

`implementation/telegram_transport.py` on ir1 matches the current dev `HEAD`, not the
published `v1.10.0` release artifact (which still has the pre-fix SOCKS5 greeting bug). This
was a deliberate, narrowly-scoped in-place fix applied without a version bump per explicit
operator instruction. **Next step:** the next tagged release (recommend `v1.10.1`) should
include this fix so the published artifact and running production code converge again —
until then, anyone re-deploying from the `v1.10.0` GitHub release artifact will NOT get this
fix automatically. See `docs/DEVELOPMENT_PROGRESS.md` for the exact diff.

## 3. No fleet-wide traffic/quota aggregation on the Web dashboard yet

The CLI's `analytics.compute_bandwidth_analytics()` already computes fleet-wide sums
(`limited_quota_sum_gb`, `limited_used_sum_gb`, `unlimited_used_sum_gb`,
`total_used_all_vps_gb`, with coverage fractions for each) — the Web Inventory page shows
only per-VPS rows plus a running/stopped/suspended count, not these fleet totals. See
`FUTURE_DEVELOPMENT_HANDOVER.md` for the full requested scope.

## 4. `is_uninformative()`'s own test coverage was added reactively

The classification bug fixed in v1.9.1 (a `state="down"` physical NIC wasn't recognized as
uninformative) existed for an unknown number of prior releases with zero test coverage —
`get_network_interfaces()`'s real-system tests only checked that the field existed, never
its actual value for a known-down interface. The fix added `hostinfo._is_uninformative()` as
a standalone, directly-tested function; worth auditing other "obviously correct" inline
boolean expressions in the same module for the same pattern.

## 5. SOCKS5 test coverage had a server-side blind spot

The existing `_FakeSocks5Server` test double replied with a fixed method selection
regardless of what the client actually offered in its greeting — meaning a client-side bug
in the offered-methods byte sequence (exactly what was found and fixed this round) could
never have been caught by the existing suite. Fixed by having the test server record and
assert on the actual offered methods. Worth auditing other hand-rolled protocol test doubles
in this codebase for the same "always replies the same regardless of input" blind spot
(e.g. the relay worker's and agent server's own protocol handlers).

## 6. Jalali month names render in a romanized/English form

E.g. "27 Aban 1405" rather than Persian script digits/month names, even when the Web UI
language is set to `fa`. This is pre-existing `host_time.py`/`jalali.py` behavior, not
addressed as part of the v1.9.0 localization pass (which covered UI chrome — headings,
labels, table headers — not date-formatting internals).

## 7. No Node.js test tooling dependency, by design — but it's a real dependency for CI

`tests/test_live_bandwidth_js.py` executes the actual shipped `live-bandwidth.js` under
Node to verify its unit-conversion math, rather than reimplementing that logic in Python.
It's correctly marked `skipif(NODE is None)` so it never breaks a Node-less CI run, but it
also means that environment silently loses coverage of the single most safety-critical piece
of client-side logic (Mbps vs. MB/s). If CI ever runs without Node, this gap should be
visible in the test summary, not just silently skipped.

## 8. No automated visual/screenshot regression testing

Every "visual verification" performed during this engagement was `curl`-based HTML
inspection (status codes, presence of expected strings/classes, structural checks) — never
an actual rendered-browser screenshot comparison. A real visual regression (a CSS rule that
breaks layout without breaking any string-level assertion) would not be caught by the
current test suite.

## 9. Backup duality can confuse an unfamiliar operator

`backup.create_full_backup_archive()` (CLI `backup-full`, Web `/backups`,
`installer.upgrade()`'s auto-backup) and a full raw filesystem tar (used ad hoc for
production deployment safety during this engagement) are BOTH called "backups" in
conversation but have very different scope — the former deliberately excludes secrets, the
latter includes everything. `docs/BACKUP_AND_RESTORE.md` and `docs/ARCHITECTURE.md` now
both document this distinction explicitly; worth considering a CLI subcommand for the raw
full-tree variant too, rather than it only existing as an ad hoc `tar` invocation.

## 10. Telegram relay/agent modules are "locally tested, not yet cross-host validated"

Per `docs/REMOTE_AND_RELAY.md` (unchanged this engagement) — both complete and unit-tested
over real sockets, but never exercised between two genuinely separate hosts over the real
internet. Still true as of this closeout.
