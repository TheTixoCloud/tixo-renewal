# TixoCloud Control Center v1.10.11 — Zero-Touch Automation Complete

`renewal_cli.VERSION = "1.10.11"`. Implements every remaining mandatory zero-touch
requirement from the production acceptance audit, in full — none deferred.

## 1. Real systemd activation for every optional module

v1.10.10 fixed this for `backup_schedule` only. This release extends the same real
activation+verification discipline to the Telegram bot, Web Control Center, relay worker,
and remote agent — all persistent `Type=simple`/no-timer services. New
`service_activation.install_and_enable_persistent_service()`/
`disable_and_remove_persistent_service()`; menu.py's four "Configure..." screens now
actually enable (and `systemctl`-verify) or disable the real service, never just saving
config and printing manual instructions. `uninstall-module` now real-disables every
optional module, not just backup_schedule.

## 2. Continuous health monitoring + bounded, safe self-healing

`readiness_notifier.py` (already running on its own timer) now also runs
`install_health_check(check_systemd=True)` every cycle. If a **core read-only** timer
(monitor/inventory-sync/hostnet-sync/readiness-notifier — never the write-capable apply
timer) is found disabled, it attempts exactly ONE real re-activation via the same
`install_and_enable_units()` the fresh-install path uses — always safe by construction,
since none of these four can write to Virtualizor. Verified for real: disabled all four
core timers on a built package in an isolated sandbox, ran the notifier, confirmed via raw
`systemctl is-enabled` (not this project's own reporting) that it genuinely re-enabled all
four, then cleaned up.

## 3. Actionable push notifications

New/recovered health problems, readiness candidates, and scheduled-backup integrity
failures now push via the same Telegram transport the backup-delivery feature already used
— with deduplication (an unchanged problem is never re-alerted) and explicit recovery
notices. Falls back to the existing local file/syslog when no push channel is configured —
nothing is ever silently dropped.

## 4. Automated first-live-renewal validation

New `first_renewal_validation.py`: `enable-auto-renewal` arms a durable per-node marker.
The next real write result from `writer.apply_renewal()` (via `apply-due`/`apply`) is
durably recorded with full expected-vs-actual detail and pushed as a notification — success
or failure. This module never retries, alters, or decides a write; `writer.py`'s existing
`UNKNOWN_NEEDS_VERIFICATION` status already structurally freezes any further attempt for
that exact (node, vpsid, period) on a mismatch. Surfaced in `doctor` under a distinct
"First live renewal validation" section, explicitly separate from service-health and
configuration-readiness.

**This directly honors the four-way distinction requested**: software acceptance (this
release), production configuration readiness (`doctor`), financial-write authorization
(`enable-auto-renewal`, still requires explicit operator confirmation, still currently
`false` in production — your deliberate safety choice, not a defect), and first-live-renewal
validation (this new mechanism, armed and waiting, never fabricating or forcing an event).

## Testing

Every new behavior's regression test was confirmed genuinely falsifiable (fails when the
fix is reverted, passes when restored) — all four persistent-service paths, self-heal
success/failure/write-capable-exclusion, health-problem dedup/recovery, first-renewal
arm/consume/exactly-once/mismatch end-to-end through real `enable_auto_renewal` +
`cmd_apply_due`, and the backup-integrity push.

Full regression suite: **1445 passed, 0 failed**. Reproducible build verified (two clean
builds, identical SHA-256).

## Deployment status

Not deployed to `ir1` or `ir2` by this session.

**Exact upgrade command:**

```bash
tixo-renewal update-apply --yes
```

After upgrading, `doctor` will show the new "First live renewal validation" section
(`not armed` until `enable-auto-renewal` is run). No other action is required for the
health-monitoring/self-heal/push features — they activate automatically on the existing
readiness-notifier timer's next run.
