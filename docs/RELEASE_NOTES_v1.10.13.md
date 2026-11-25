# TixoCloud Control Center v1.10.13 — Independent Watchdog, Multi-Node Audit, Creation Dates, Final Acceptance

`renewal_cli.VERSION = "1.10.13"`. Final acceptance release following a comprehensive
product audit. No unrelated features, no architectural rewrites — strictly the items
below.

## 1. Independent readiness-notifier watchdog

The readiness notifier's own self-heal (added v1.10.11) cannot detect its own disablement,
since a disabled timer never fires to heal itself. `service_activation.watchdog_check_readiness_notifier`
is now called from a *different* timer (the monitor's `dry-run`) to detect and re-enable the
notifier if it is disabled or inactive.

- Gated behind `--check-notifier-watchdog`, off by default (mirrors `install_health_check`'s
  existing `check_systemd=False` convention) — an earlier, unconditional version of this call
  was caught during testing writing real systemd unit files; fixed before merge.
- Respects intentional operator shutdowns: new `disable-readiness-notifier` /
  `enable-readiness-notifier` CLI commands create/remove a `NOTIFIER_DISABLED_BY_OPERATOR`
  marker file that the watchdog checks first and never overrides.
- The watchdog can only re-enable a systemd unit; it has no path to WRITE_APPROVED or any
  financial-write capability.

## 2. Multi-node awareness audit

`tui_textual.py` had a hardcoded `node_id="self"` in two places (app init, VPS detail
screen); both now derive the real configured node. Every other node_id usage in the
codebase audited and confirmed to be a safe *default parameter* matching the only node_id
any real host-local install ever has.

## 3. VPS creation date visibility

Creation date is now shown in both the VPS detail view (added earlier this cycle) and the
inventory **list** (new "Created" column, extra-wide terminal tier ≥120 columns only — never
disturbs existing 100–119 column layouts). Honors Gregorian/Jalali display-timezone
preference. Reports "unknown" rather than fabricating a date when unverified. Fixed a real
ANSI-escape-width alignment bug found while building this feature (progress-bar color codes
were inflating measured column width).

## 4. External repository review

`LivingG0D/virtualizor-bwreset` reviewed in full. No changes adopted — no capability it has
that this project lacks.

## 5. Packaged-artifact lifecycle testing

Install, reinstall, uninstall (standard + purge), and a genuine v1.10.12→v1.10.13 offline
upgrade were all tested against real downloaded/built release artifacts (not dev source):

- Standard uninstall preserves backups/config/data; purge removes config+ledger but still
  preserves backups unless a separate delete-backups flag is given.
- The offline upgrade test simulated IR1's real production ledger (VPS 62/63/64 APPLIED)
  before upgrading, then confirmed after upgrade: version correctly reports 1.10.13, the
  three operations rows are byte-for-byte unchanged (no replay, no duplication), and
  `nodes.json`/`policy.json` are byte-identical.

## 6. Full interactive acceptance pass

All 29 menu sections exercised against the real packaged artifact via direct function/CLI
calls with real input/getpass mocking.

## 7. Regression and build

Full suite: 1463 passed. Reproducible build confirmed via two independent builds producing
an identical SHA-256.

## Production deployment (2026-11-25)

Deployed to both IR1 and IR2 via checksum-verified offline packages. Both nodes' pre-upgrade
state (VPS 62/63/64 already-APPLIED renewals, WRITE_APPROVED, stored credentials, all six
systemd timers) was independently re-verified before and after the upgrade on both hosts.
No replay, no duplicate writes, no customer VPS interruption, no reboot.
