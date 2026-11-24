# Known Limitations & Technical Debt

Honest list, compiled at project closeout (v1.10.2, updated from the v1.10.1 version of this
file). Each item notes impact and suggested next step.

## 1. `deploy/install.sh`'s `APP_VERSION` detection line — RESOLVED in v1.10.1

Was broken for several releases (fixed in dev at commit `4c5569b` during the v1.10.0
engagement, but not shipped in a tagged release until `v1.10.1`). Confirmed via live
reproduction on a second hypervisor (ir2): running `install.sh` from the published v1.10.0
release artifact crashed with `ModuleNotFoundError: No module named 'renewal_cli'`
immediately after Virtualizor detection (code copy itself succeeded; the crash happened on
the version-check line, before systemd template generation and wrapper (re)creation — a
genuine partial install). `v1.10.1` ships the fix and adds
`test_install_sh_fresh_install_succeeds_end_to_end` / `test_install_sh_second_run_recovers_and_stays_idempotent`
(both run the real `install.sh` script as a subprocess, closing the test gap that let this
ship unnoticed in the first place — every prior install-focused test called
`renewal_cli.py install` directly, bypassing `install.sh` entirely). **ir2 still needs a
fresh install run with the v1.10.1 artifact** — not done automatically, per explicit
instruction.

## 2. Production/release convergence — RESOLVED in v1.10.1

`implementation/telegram_transport.py`'s SOCKS5 greeting fix (applied in-place to ir1
without a version bump during the v1.10.0 engagement) now ships in the `v1.10.1` tagged
release — the published artifact and ir1's running code converge again as of this release.

## 3. `README.md`'s install command was nine releases stale — RESOLVED in v1.10.1

Was pinned to `v1.1.0` (the SECOND-ever release) while every actual install still worked via
`deploy/bootstrap.sh`'s own correctly-updated default tag — found during a release-process
audit, not by any test, because no test checked `README.md` (only `bootstrap.sh`'s own
default tag had a drift test). Fixed, plus a new
`test_readme_install_command_matches_current_version` regression test so this specific class
of staleness can't recur silently.

## 4. No fleet-wide traffic/quota aggregation on the Web dashboard yet

The CLI's `analytics.compute_bandwidth_analytics()` already computes fleet-wide sums
(`limited_quota_sum_gb`, `limited_used_sum_gb`, `unlimited_used_sum_gb`,
`total_used_all_vps_gb`, with coverage fractions for each) — the Web Inventory page shows
only per-VPS rows plus a running/stopped/suspended count, not these fleet totals. See
`FUTURE_DEVELOPMENT_HANDOVER.md` for the full requested scope.

## 5. `is_uninformative()`'s own test coverage was added reactively

The classification bug fixed in v1.9.1 (a `state="down"` physical NIC wasn't recognized as
uninformative) existed for an unknown number of prior releases with zero test coverage —
`get_network_interfaces()`'s real-system tests only checked that the field existed, never
its actual value for a known-down interface. The fix added `hostinfo._is_uninformative()` as
a standalone, directly-tested function; worth auditing other "obviously correct" inline
boolean expressions in the same module for the same pattern.

## 6. SOCKS5 test coverage had a server-side blind spot

The existing `_FakeSocks5Server` test double replied with a fixed method selection
regardless of what the client actually offered in its greeting — meaning a client-side bug
in the offered-methods byte sequence (exactly what was found and fixed this round) could
never have been caught by the existing suite. Fixed by having the test server record and
assert on the actual offered methods. Worth auditing other hand-rolled protocol test doubles
in this codebase for the same "always replies the same regardless of input" blind spot
(e.g. the relay worker's and agent server's own protocol handlers).

## 7. Jalali month names render in a romanized/English form

E.g. "27 Aban 1405" rather than Persian script digits/month names, even when the Web UI
language is set to `fa`. This is pre-existing `host_time.py`/`jalali.py` behavior, not
addressed as part of the v1.9.0 localization pass (which covered UI chrome — headings,
labels, table headers — not date-formatting internals).

## 8. No Node.js test tooling dependency, by design — but it's a real dependency for CI

`tests/test_live_bandwidth_js.py` executes the actual shipped `live-bandwidth.js` under
Node to verify its unit-conversion math, rather than reimplementing that logic in Python.
It's correctly marked `skipif(NODE is None)` so it never breaks a Node-less CI run, but it
also means that environment silently loses coverage of the single most safety-critical piece
of client-side logic (Mbps vs. MB/s). If CI ever runs without Node, this gap should be
visible in the test summary, not just silently skipped.

## 9. No automated visual/screenshot regression testing

Every "visual verification" performed during this engagement was `curl`-based HTML
inspection (status codes, presence of expected strings/classes, structural checks) — never
an actual rendered-browser screenshot comparison. A real visual regression (a CSS rule that
breaks layout without breaking any string-level assertion) would not be caught by the
current test suite.

## 10. Backup duality can confuse an unfamiliar operator

`backup.create_full_backup_archive()` (CLI `backup-full`, Web `/backups`,
`installer.upgrade()`'s auto-backup) and a full raw filesystem tar (used ad hoc for
production deployment safety during this engagement) are BOTH called "backups" in
conversation but have very different scope — the former deliberately excludes secrets, the
latter includes everything. `docs/BACKUP_AND_RESTORE.md` and `docs/ARCHITECTURE.md` now
both document this distinction explicitly. **Partially addressed in v1.10.2**:
`updater.create_full_filesystem_backup()` formalizes the raw full-tree variant as a real,
tested function (used automatically by `update-apply` before every update) — but it is still
only reachable through the updater, not as a standalone operator-invokable CLI subcommand of
its own.

## 12. v1.10.2 in-app updater and uninstaller are untested against two specific real-world
    upgrade paths

`update-apply`'s full CLI flow is covered end-to-end against fixture-built "new version"
tarballs (see `tests/test_renewal_system.py` and `tests/test_updater.py`), including
checksum failure, health-check-triggered rollback, interrupted downloads, missing
dependencies, and import errors during the post-update step. What remains unverified as of
this closeout: an actual upgrade run against the **real, published** v1.10.2 release
artifact starting from genuine v1.10.0-shaped and v1.10.1-shaped install directories (as
opposed to a synthetic fixture), and both uninstall modes run against that same published
artifact rather than the in-repo `implementation/` modules. See the closeout report for
which of these were actually exercised before publishing.

## 11. Telegram relay/agent modules are "locally tested, not yet cross-host validated"

Per `docs/REMOTE_AND_RELAY.md` (unchanged this engagement) — both complete and unit-tested
over real sockets, but never exercised between two genuinely separate hosts over the real
internet. Still true as of this closeout.

## 13. v1.10.3 hardening round: real root causes fixed, but full mission scope not claimed complete

This round was explicitly scoped by the operator as a large, multi-section hardening
mission (installer automation, write-authorization unification, full renewal-state-machine
audit, all 29 menu options, CI/CD quality gates, multi-distro testing, full security audit,
documentation). What was genuinely implemented, tested, and verified this round:

- Automatic systemd activation of the 4 read-only core timers (`service_activation.py`,
  `install-activate-core-timers`), replacing the error-prone manual `cp`/`systemctl`
  sequence that caused the exact reported `ir2` symptoms (missed units, enabled-but-not-
  persisted state, no trustworthy health check). Verified against this project's own real,
  functional systemd (not mocked) in `tests/test_deploy_package.py`, including a genuine
  activate → verify → uninstall round-trip.
- Unified production-write authorization (`enable-auto-renewal`/`disable-auto-renewal`,
  `installer.enable_auto_renewal`/`disable_auto_renewal`) replacing three previously
  separate, undocumented manual steps.
- Two real, previously-undiscovered bugs found and fixed BY running the above against real
  systemd rather than only against mocks: `cmd_apply_due`'s exit-1-for-a-safety-refusal
  (now exit 3 + `SuccessExitStatus=3`), and `cmd_inventory_sync`'s exit-1-for-not-yet-
  configured (now exit 0 + `NOT_CONFIGURED`) — the second of which would have made the
  newly-auto-enabled inventory-sync timer show "failed" on every single fresh install
  before `store-credentials` was run, had it shipped unfixed.
- A targeted security scan (shell=True/os.system/eval/exec/pickle/yaml.load usage,
  subprocess argument-list discipline, tempfile handling, secret-file permission
  enforcement) found no new issues; the existing codebase's established discipline
  (list-args-only subprocess calls, GPG passphrases via stdin fd never argv, 0600 secret
  files, path-traversal-safe tempfile suffixes) was confirmed intact, not re-derived from
  scratch.
- Existing regression coverage for unlimited-VPS exclusion, frozen-candidate safeguards,
  and emergency-grant cooldown/eligibility (21 pre-existing tests in
  `test_emergency_policy.py` alone) was confirmed still passing, not rewritten.

What was explicitly NOT attempted at full rigor this round, and should not be assumed done:
a line-by-line audit of all 29 individual menu options beyond what the above changes
touched; a rebuilt CI/CD pipeline with static analysis, type checking, or dependency
vulnerability scanning (none of these tools were added — the existing pytest suite remains
the only automated gate); testing on any Linux distribution other than this project's one
verified target (Ubuntu 24.04); a full renewal-state-machine audit beyond what the
pre-existing test suite already covers (timezone/leap-year/month-end edge cases were not
independently re-verified this round). See the v1.10.3 closeout report for the exact,
itemized final status.

## 14. v1.10.4: real duplicate-renewal race found and fixed; production investigation blocked at the network level

During a follow-up mission specifically scoped to verify duplicate-renewal safety, a real,
reproducible bug was found in `writer.apply_renewal()`: the dedup check and the claim were
two separate, non-atomic SQL statements, so two genuinely concurrent processes could both
pass the check and both insert a claim row for the identical `(node_id, vpsid, period_key)`
-- meaning both would proceed to write to Virtualizor for the SAME renewal period. This was
directly reproduced (two sqlite3 connections to one file, racing the exact two statements)
before being fixed with a UNIQUE index + IntegrityError handling; the matching race in
`retry_deferred_renewal()` was fixed the same way. See `docs/RELEASE_NOTES_v1.10.4.md` for
full detail. The default monthly gift was also corrected from 100 GB to 0 GB to match the
actual carry-over business model (see that release's own notes).

**Production investigation (Phase Two of that mission) was attempted and found blocked**,
not by missing credentials but by network connectivity: the engineering environment used
had no route at all (confirmed via `nc`/`curl`, while general internet access worked fine)
to either `ir1.tixocloud.com` or the pinned, previously-trusted `87.107.114.194` endpoint
for `ir1`. Separately, and independent of that network block: `ir1.tixocloud.com`'s SSH
host key had changed from what was previously trusted in `known_hosts` -- this was correctly
NOT bypassed (no `StrictHostKeyChecking=no`, no edited `known_hosts`) since there was no way
to verify out-of-band from within that environment whether this reflects a legitimate host
rebuild/DNS change or something adversarial. **This must be resolved by an operator with
direct access before any future automated or manual SSH connection to that hostname** --
either confirm the new key against the actual current host configuration, or investigate
why it changed. No endpoint for `ir2` was ever found configured anywhere in the available
project configuration (no SSH alias, no documented hostname/IP) -- guessing one was
correctly avoided rather than attempted. No production investigation, baseline snapshot, or
deployment was therefore possible this round; all work was repository-level only.
