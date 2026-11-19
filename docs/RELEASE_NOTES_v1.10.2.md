# TixoCloud Control Center v1.10.2 — Real Updater, Auto-Launch Install, Two-Mode Uninstaller

`renewal_cli.VERSION = "1.10.2"`. Delivers the three remaining pieces of a genuinely
self-contained release lifecycle: one-command install that opens the management menu for
you, an in-app updater that actually updates instead of just reporting a version string, and
a complete two-mode uninstaller. No Virtualizor/VPS/quota/network-interface/renewal-policy
behavior changes.

## What's in this release (since v1.10.1)

### A. Auto-launch management menu after install

`deploy/install.sh` now opens the `tixo-renewal` menu automatically immediately after a
successful install, when run from a genuinely interactive terminal — including the piped
`curl | bash` one-liner, where stdin is already consumed by the script's own source.
Detection uses `[ -t 1 ]` (stdout) plus explicit `/dev/tty` read/write access (never `[ -t 0 ]`,
which is meaningless in a piped-script context), and the launched process's stdin/stdout/
stderr are explicitly redirected to `/dev/tty` so `input()`/`getpass()` work correctly. A
failed install never launches the menu. Set `TIXO_RENEWAL_NO_AUTO_MENU=1` to suppress this
for scripted/CI installs.

### B. Real in-app updater (`implementation/updater.py`, new)

`tixo-renewal`'s menu "Check for updates" and the new `update-check`/`update-apply` CLI
commands now perform a genuine update, not just a version comparison:

- `update-check` queries the latest published GitHub release and reports whether a newer
  version is available — read-only, never downloads or modifies anything.
- `update-apply` re-fetches the target release's asset URLs, downloads the package tarball
  and `SHA256SUMS` to a temp directory, and verifies the checksum **before touching anything**
  in the install directory. Only then does it take a full raw filesystem backup (everything,
  including secrets — distinct from `backup.create_full_backup_archive()`'s deliberately
  secret-excluding format), swap in the new `lib/`/`bin/`/`lib/static/`, run the idempotent
  config/schema upgrade step, and run a health check. `renewal-config/` (policy, nodes,
  secrets, `WRITE_APPROVED`) and `shadow-monitor-state/` (the ledger) are never touched.
  Already-active Web/Telegram services are restarted afterward. On any failure at any stage
  — bad checksum, failed download, a missing dependency during the install step, a broken
  import during the health check — the previous `lib/`/`bin/` are restored exactly, and the
  function reports `ok=False` with the failing stage and error; it never raises and never
  leaves the install directory in a half-updated state. Requires `--yes` or interactive
  confirmation; never applies silently. The menu re-execs the running process
  (`os.execv`) after a successful update so it reopens the *updated* menu, not stale
  in-memory bytecode.

### C. Complete two-mode uninstaller (`implementation/uninstaller.py`, new)

- `uninstall-preview` shows exactly what each mode will remove/preserve before anything runs.
- `uninstall` (standard mode) stops and disables only the app's own systemd units (a fixed,
  exact 16-unit allowlist — never a glob match), removes app code, and preserves
  `renewal-config/`, `shadow-monitor-state/`, and existing backups.
- `uninstall --purge` removes everything app-owned, including config/secrets/the ledger and
  (optionally, with `--delete-backups`) backups — gated behind typing the exact confirmation
  phrase `PURGE ALL TIXOCLOUD DATA`, shown by `uninstall-preview` first.
- Both modes verify ownership before deleting anything: a directory is only treated as "ours"
  if both its name matches AND it contains a known signature file (e.g. `lib/` must contain
  both `renewal_cli.py` and `menu.py`) — directory name alone is never sufficient grounds for
  deletion. An unrelated `.service` file or an operator's own notes file sitting inside the
  install directory survives untouched in both modes. Handles partially-installed app states
  (missing directories, already-stopped services) without erroring. The menu calls
  `sys.exit()` after either mode completes, since both delete `lib/menu.py` itself —
  returning to the normal menu loop would run stale in-memory code.

## New regression tests (this release)

- `tests/test_updater.py` (18 tests) — fetch/download/checksum unit tests; full fixture-driven
  `apply_update` integration tests covering success, bad checksum, failed-health-check
  rollback, missing release assets, an interrupted download mid-transfer, a missing
  dependency during the post-update install step, and a broken import during the health
  check.
- `tests/test_uninstaller.py` (22 tests) — every `uninstaller.py` function, including the
  ownership-verification guard and the fixed-allowlist systemd/filesystem matching.
- `tests/test_menu.py` — `action_check_updates` (5 tests) and `action_uninstall` (7 tests,
  all asserting the process actually exits).
- `tests/test_deploy_package.py` — real-PTY tests of `install.sh`'s auto-launch behavior
  (genuine pseudo-terminal via `pty.openpty()`, not a `subprocess.run()` simulation, since
  the latter never exercises the `[ -t 1 ]` code path at all) plus CLI-level uninstall tests
  that build and run the real published-shaped package.
- `tests/test_renewal_system.py` — full CLI-level `update-check`/`update-apply` flow test
  (config/secret preservation, service-restart verification) that caught the late-binding
  default-argument bug below.

## Bug fixed during this release's own testing

`apply_update()`'s `download_fn=download_file, fetch_fn=fetch_release_info` default
parameter values were bound to the original function objects at module-import time (standard
Python default-argument-evaluated-once-at-definition-time semantics) — so a caller relying on
the defaults (as `cmd_update_apply` does) could not be redirected by monkeypatching the
module attributes, and in testing this caused a real, unwanted network call. Fixed by
resolving both lazily inside the function body, mirroring the pattern already used for
`run_install_upgrade_fn`/`health_check_fn` two parameters later in the same signature.

## Scope discipline

No `policy.py`/`analytics.py`/renewal-decision logic changed. No systemd timer *schedule*
changed (only which units the uninstaller is allowed to touch, via the existing fixed
allowlist). No Virtualizor API call added or changed.

## Testing

Full regression suite: **1374 passed, 0 failed**. Reproducible build verified (two clean
builds, identical `SHA256SUMS`). See `docs/KNOWN_LIMITATIONS.md` §12 for what remains
verified only against fixtures rather than the published artifact at the time these notes
were written, and the closeout report for which of those were subsequently exercised before
publishing.

## Deployment status

Not deployed to `ir1` or `ir2` as part of this release. See
`FUTURE_DEVELOPMENT_HANDOVER.md` §17 for the prepared (not executed) upgrade procedure for
both hosts.
