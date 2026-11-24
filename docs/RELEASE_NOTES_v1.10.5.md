# TixoCloud Control Center v1.10.5 — Legacy Bandwidth-Automation Conflict Detection

`renewal_cli.VERSION = "1.10.5"`. Adds a read-only safeguard found necessary during a real
production audit of `ir1`/`ir2`: detection of foreign, write-capable bandwidth-automation
cron jobs, so `enable-auto-renewal` can never silently run alongside one unnoticed.

## What's in this release (since v1.10.4)

### Legacy bandwidth-automation detection

During a read-only production audit of `ir1` and `ir2`, a standalone, pre-existing
script-based bandwidth carry-over system was found already installed on both hosts via
`/etc/cron.d` -- a read-only monitor/ledger/guard trio (never writes to Virtualizor) plus a
separate, disabled-but-present script that, when enabled, writes directly to Virtualizor's
`vps.bandwidth` column via MySQL. Two independently-unaware automatic writers running at
once is exactly how a renewal gets double-applied.

New `installer.detect_legacy_bandwidth_automation()`: scans `/etc/cron.d` (never reads file
*contents* -- only names, against a small set of known patterns) and reports any matching
job as active or disabled, flagging whether an active one looks write-capable. **Never
disables, deletes, or modifies anything it finds** -- detection only, per explicit
instruction to leave any such pre-existing operator tooling untouched.

Wired in three places:
- `install-health-check` now reports `legacy_bandwidth_automation` for visibility, always
  (never affects the overall `ok` status -- presence alone isn't unhealthy).
- `cmd_install` prints a one-time informational notice if anything matching is found.
- **`enable-auto-renewal` (CLI) and the menu's "Enable automatic renewal" screen now refuse
  by default** if an active, write-capable foreign job is detected, requiring an explicit
  `--override-legacy-write-conflict` (CLI) or a separate, clearly-worded confirmation (menu)
  before proceeding -- and even then, the foreign job itself is never touched.

## New regression tests

`test_installer.py`: empty/missing cron dir, unrelated cron files ignored, read-only
monitor/ledger/guard correctly classified as non-write-capable, an active write-capable job
correctly flagged, a `.disabled`-suffixed job correctly classified as inactive,
`enable_auto_renewal` refusing and then proceeding-with-override against an injected
disposable cron directory (never the real `/etc/cron.d`).

## Production investigation status

Read-only audits of both `ir1` and `ir2` were completed this round (SSH host-key
fingerprints independently verified against operator-supplied values for `ir1`; TOFU
accepted for `ir2`'s first-ever connection with no prior record to compare against). No
production system was modified. See the closeout report for full findings, including:
Virtualizor schema confirmation (`vps.bandwidth int(10)`, `vps.used_bandwidth
decimal(12,2)`, matching this project's existing assumptions exactly), differing host
timezones (`ir1`: Asia/Tehran +03:30, `ir2`: UTC -- confirmed safe, since this project's
renewal-decision code uses only `datetime.now(timezone.utc)`, never a naive/local call),
and the full legacy-automation inventory on each host.

## Testing

Full regression suite: **1394 passed, 0 failed**.

## Deployment status

Not deployed to `ir1` or `ir2`. Per explicit instruction, final installation on both
production hosts is reserved exclusively for the operator, using the official one-line
install/update commands below.
