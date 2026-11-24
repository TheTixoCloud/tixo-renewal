# TixoCloud Control Center v1.10.7 — Fixed Global Wrapper Upgrade Regression

`renewal_cli.VERSION = "1.10.7"`. Fixes a real, confirmed production regression: after a
genuine v1.10.5 -> v1.10.6 upgrade on IR1, `renewal_cli.VERSION` correctly reported the new
release, but `tixo-renewal doctor` (and every other new subcommand) still launched the
interactive menu instead.

## Root cause

The global `/usr/local/bin/tixo-renewal` wrapper's dispatch logic (added in v1.10.6) existed
**only** as a bash heredoc inside `deploy/install.sh`. `update-apply`'s entire upgrade path
is pure Python (download → verify → backup → swap `lib/`+`bin/` → run `installer.upgrade()`
→ health check) and never re-invokes `install.sh` — so the wrapper script already on disk
was never touched by any upgrade, no matter what version of the application code it ended
up pointing at.

## Fix

Moved wrapper generation entirely into Python: `installer.write_global_wrapper()`, called
from `cmd_install()` — which both a fresh install **and every upgrade path**
(`install-upgrade`, the menu's install wizard, and `update-apply`'s
`run_install_upgrade_fn`, all three of which already call `installer.upgrade()` →
`cmd_install()`) now go through. `install.sh`'s former bash heredoc — a second
implementation of the identical logic that was never kept in sync — is removed entirely.
There is now exactly one implementation, regenerated on every install and every upgrade.

**Verified the fix is real, not just a passing test**: wrote the regression test, then
temporarily reverted the fix and re-ran it to confirm it genuinely fails against the old
behavior, then restored the fix and confirmed it passes again.

## Also fixed: outdated/contradictory instructions

Per direct user report: numerous printed messages (in `renewal_cli.py`, `menu.py`,
`install.sh`) and documentation (`README.md`, `docs/INSTALL_GUIDE.md`,
`docs/DEPLOYMENT.md`, `docs/PRODUCTION_ACTIVATION.md`, `FUTURE_DEVELOPMENT_HANDOVER.md`)
still told operators to invoke `renewal_cli.py <command>` directly or manually export
`PYTHONPATH`/`TIXO_RENEWAL_INSTALL_DIR` — stale advice from before the global wrapper
existed at all. All now consistently recommend `tixo-renewal <command>`.

## New tests

`test_update_apply_regenerates_a_stale_global_wrapper` — reproduces the exact stale-wrapper
state found on IR1 (the original fresh-install wrapper, unconditionally exec'ing `menu.py`,
with no dispatch logic at all), runs the real `cmd_update_apply` CLI command against it, and
proves the wrapper is regenerated with current, dispatch-aware content.

## Testing

Full regression suite: **1417 passed, 0 failed**. Reproducible build verified (two clean
builds, identical SHA-256).

## Production investigation

Confirmed the bug directly on the real IR1 installation (read-only inspection: compared the
wrapper's mtime/content against the installed `renewal_cli.VERSION`) before writing any
fix. No production system was modified.

## Deployment status

Not deployed to `ir1` or `ir2` by this session. IR1 is currently on v1.10.6 with the stale
wrapper; upgrading to this release via `renewal_cli.py update-apply --yes` (the full-path
form is required for this ONE upgrade specifically, since v1.10.6's own wrapper doesn't
dispatch correctly yet) will fix it permanently — every upgrade from this point on
regenerates the wrapper automatically.
