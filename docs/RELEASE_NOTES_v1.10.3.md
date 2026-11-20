# TixoCloud Control Center v1.10.3 — Automatic Timer Activation & Unified Write Authorization

`renewal_cli.VERSION = "1.10.3"`. A hardening release aimed squarely at the manual-
troubleshooting burden a real second-host installation (`ir2`) actually hit: missing
timers, enabled-but-not-persisted units, a confusing multi-step production-write switch,
and no trustworthy single health check. No Virtualizor API behavior, renewal-decision
logic, or database schema changed.

## What's in this release (since v1.10.2)

### A. Automatic systemd activation of the read-only core timers

`deploy/install.sh` now installs and enables the 4 READ-ONLY core timers — monitor,
inventory-sync, hostnet-sync, readiness-notifier — into real systemd automatically, when
run as root: copies the unit files into `/etc/systemd/system`, runs `daemon-reload`, then
`enable --now` on each timer. This is exactly the manual, error-prone `cp`/`systemctl`
sequence a real installation previously required by hand, one unit at a time — which is
how `inventory-sync` and `hostnet-sync` ended up missed, and some timers ended up active
without being enabled for reboot persistence. None of these four timers can write to
Virtualizor, by construction. Opt out with `TIXO_RENEWAL_NO_AUTO_SYSTEMD=1`; re-run
yourself anytime with the new `install-activate-core-timers` command (idempotent, root
required). The write-capable `apply` timer is deliberately never touched by this — see B.

New module: `implementation/service_activation.py`.

### B. Unified production-write authorization

Before this release, "enabling automatic renewal" meant three separate, undocumented
manual steps (creating `WRITE_APPROVED`, editing `policy.json`'s `auto_renewal_enabled`,
and separately finding and installing the `apply` timer) — exactly the kind of confusing,
multi-step experience that led to "expected write-authorization failures" being
misdiagnosed as systemd crashes. Now one confirmed action does all three together:

```
renewal_cli.py enable-auto-renewal --yes     # or interactive confirmation
renewal_cli.py disable-auto-renewal          # always reverses all three together
```

The menu's "Enable automatic renewal"/"Disable automatic renewal" screens now do exactly
this (previously they only ever flipped the `policy.json` flag). New shared implementation:
`installer.enable_auto_renewal()`/`disable_auto_renewal()`.

### C. Two real bugs found and fixed by testing A and B against genuine systemd

- `cmd_apply_due`'s "WRITE_APPROVED missing" safety refusal returned a generic exit 1,
  making a deliberate, by-design block indistinguishable from an actual crash in
  `systemctl status`/journalctl. Now exit 3, with the `apply` unit declaring
  `SuccessExitStatus=3`.
- `cmd_inventory_sync` hard-failed with exit 1 whenever credentials hadn't been stored yet
  — meaning the newly-auto-activated `inventory-sync` timer would show **failed** on every
  single fresh install until an operator ran `store-credentials`, every 15 minutes, for as
  long as that took. Now reports `status=NOT_CONFIGURED` and exits 0. (`hostnet-sync` and
  `readiness-notifier` already handled their own "not configured yet" cases correctly;
  `monitor`'s `dry-run` already handles a missing Virtualizor gracefully too — only
  `inventory-sync` had this gap.)

Both were only found because the new activation code was tested against this project's own
genuinely running systemd (not a mock) — see the new real-systemd integration tests below.

### D. Trustworthy, systemd-aware health check

`install-health-check` (CLI and the menu's install-wizard screen) now optionally verifies
real systemd state — `systemctl is-enabled`/`is-active`/`is-failed` for every core
timer/service — not just that config files parse. Deliberately **not** wired into
`updater.apply_update`'s own automatic rollback gate (which must keep working for installs
that predate this automatic activation, including real pre-v1.10.3 `ir1`/`ir2`-shaped
installs); see `installer.health_check`'s docstring for the exact reasoning.

## Security review

A targeted scan for `shell=True`, `os.system`, `eval`/`exec`, `pickle`, `yaml.load`, and
subprocess argument-list discipline found no issues, new or pre-existing — this codebase's
established discipline (list-args-only subprocess calls, GPG passphrases piped via stdin fd
rather than argv, 0600 secret-file permissions, path-traversal-safe tempfile suffixes) was
confirmed intact. All of this project's own new code (`service_activation.py`,
`installer.py`'s new functions) only ever passes fixed, internally-defined systemd unit
names to `systemctl` — never operator-supplied strings.

## New/updated tests

- `tests/test_installer.py` — systemd-aware `health_check()` branch (enabled/active/failed
  reporting), using the project's established `_systemctl` mock-point convention (never
  touching the real host from this test file).
- `tests/test_deploy_package.py` — a genuine real-systemd round-trip integration test
  (`install-activate-core-timers` → verify via real `systemctl` → idempotent re-run →
  `install-health-check`), a real-systemd uninstall test confirming `uninstall --yes`
  correctly removes genuinely-activated units, and a regression test for the
  `TIXO_RENEWAL_NO_AUTO_SYSTEMD` opt-out. Every other install.sh-invoking test in this file
  now explicitly sets that opt-out so the automated suite never touches the real
  test-runner host's systemd except in these two dedicated tests (both clean up in a
  `finally` block regardless of outcome).
- `tests/test_renewal_system.py` — regression tests for both exit-code fixes (C above).

## Scope discipline, stated honestly

This release fixes the concrete, reproduced `ir2` installation defects and unifies
write-authorization. It does **not** claim: a line-by-line audit of all 29 menu options
beyond what these changes touched; a new CI/CD pipeline with static analysis, type
checking, or dependency scanning; verification on any Linux distribution beyond this
project's one verified target (Ubuntu 24.04); or a fresh audit of renewal-state-machine
edge cases beyond the pre-existing (already passing) regression suite. See
`docs/KNOWN_LIMITATIONS.md` §13 for the complete, itemized scope statement.

## Testing

Full regression suite: **1381 passed, 0 failed**. Reproducible build verified (two clean
builds, identical `SHA256SUMS`).

## Deployment status

Not deployed to `ir1` or `ir2` as part of this release. See
`FUTURE_DEVELOPMENT_HANDOVER.md` §17 for the prepared (not executed) upgrade procedure for
both hosts — note that, as with v1.10.2, a host still on v1.10.0/v1.10.1 cannot use
`update-apply` to reach this release directly (that command didn't exist yet on those
versions); the first hop must go through the bootstrap/`install.sh` path.
