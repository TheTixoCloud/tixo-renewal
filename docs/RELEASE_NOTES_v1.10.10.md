# TixoCloud Control Center v1.10.10 — Real Backup-Timer Activation

`renewal_cli.VERSION = "1.10.10"`. Fixes a confirmed usability defect reported after real
operator use of the interactive menu's "Configure Backup Schedule (optional)" screen (option
22): after explicitly confirming "Enable the scheduled backup timer now?" and "Save this
backup schedule?", the software only generated systemd unit *templates* and printed manual
`cp`/`daemon-reload`/`enable` instructions — "Saved" was reported as if it meant "running,"
which it did not.

## Fix

Reuses `service_activation.install_and_enable_units()`/`disable_and_remove_units()` — the
same real-activation helpers the four read-only core timers have used automatically since
v1.10.3. Enabling the backup schedule now actually copies the unit files into
`/etc/systemd/system`, runs `daemon-reload`, and `enable --now`s the timer — then **verifies**
via `systemctl is-enabled`/`is-active` before reporting success, and reports a real failure
(not a silent "saved") if activation didn't genuinely happen. Disabling — via the menu or
`uninstall-module --module backup_schedule` — now equally really stops and removes the real
systemd state, not just the generated template file.

## Verified end-to-end against real systemd

In an isolated sandbox (not IR1/IR2): installed the actual built v1.10.10 package, drove the
real menu action through its real input prompts, and confirmed via raw `systemctl
is-enabled`/`is-active` (not this project's own reporting) that the timer was genuinely
enabled and running. Then ran `uninstall-module --module backup_schedule` and confirmed via
the same raw `systemctl` check that it was genuinely gone. Cleaned up afterward.

## New regression tests, confirmed genuinely falsifiable

Four new tests (menu enable/failure paths, installer disable/failure paths) — each confirmed
by temporarily reverting the fix and re-running: fails without it, passes with it restored.
Also found and documented a real pre-existing gap while writing these:
`install_and_enable_units()` (used by core-timer activation since v1.10.3) had **zero** test
coverage anywhere in the suite — visible in `test_installer.py`'s own comment referencing a
`test_service_activation.py` file that does not actually exist.

## Known, deliberate scope limit

The other optional modules (Telegram bot, Web Control Center, relay worker, remote agent)
still only print `tixo-renewal <x>-run` as a manual follow-up step — this is architecturally
different (long-running processes with no generated `.timer`, not periodic jobs) and the
current messaging is honest (never claims activation it didn't perform), so it was left
out of this release's scope. A future release could extend real systemd `.service`
installation to these too for full zero-touch parity.

## Testing

Full regression suite: **1422 passed, 0 failed**. Reproducible build verified (two clean
builds, identical SHA-256).

## Production investigation

Read-only SSH inspection of both IR1 and IR2 was performed as part of a broader production
acceptance audit (see the full audit report delivered alongside this release). No production
system was modified.

## Deployment status

Not deployed to `ir1` or `ir2` by this session.

**Exact upgrade command:**

```bash
tixo-renewal update-apply --yes
```
