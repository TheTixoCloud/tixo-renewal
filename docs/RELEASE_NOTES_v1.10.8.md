# TixoCloud Control Center v1.10.8 — Fixed the Real Cause of the Wrapper-Upgrade Regression

`renewal_cli.VERSION = "1.10.8"`. v1.10.7 shipped a fix for the stale-global-wrapper
regression that was real but **incomplete**: a genuine, manual, subprocess-level upgrade
test against the actual published v1.10.7 release (not a same-process unit test) showed the
wrapper was *still* stale after upgrading to v1.10.7, despite `VERSION` correctly reporting
"1.10.7".

## Root cause (the deeper one)

`updater.apply_update()`'s default `run_install_upgrade_fn` called back into
`installer.upgrade()` → `cmd_install()` **in-process**, via `import installer as inst`. But
the process running `apply_update()` was started *before* `lib/`+`bin/` were swapped to the
new release's files a few lines earlier in the same function — so `import installer`
returned the **already-cached, pre-update** module object from `sys.modules`, not the new
code just written to disk. Re-importing a module that's already in `sys.modules` never
reloads it. Any newly-added install-step logic in a just-downloaded release — including
v1.10.7's own `write_global_wrapper()` call — never actually ran during a real cross-version
upgrade.

**This class of bug is invisible to same-process unit tests.** A test that builds a "new"
package and calls the upgrade path within the same pytest process is always "current code
calling current code" — there is no genuine old-process/new-files split for it to catch.
Only a real subprocess-level test, run against an actually-published release, exposed it.

## Fix

`updater.py`'s default install step (`_run_post_update_install_step`) now runs the
newly-swapped-in `bin/renewal_cli.py install` as a genuine `subprocess.run()` — a real,
separate process import of the just-downloaded release's own code, immune to whatever stale
modules the parent process has cached. This guarantees any future install-step logic a
release adds will actually execute during a real upgrade, not just a fresh install.

`installer.write_global_wrapper()` also now honors a `TIXO_RENEWAL_WRAPPER_PATH` environment
variable override (read fresh, every call), so test harnesses (and operators with
non-standard install prefixes) can redirect the wrapper path without touching the real host
default.

## Verified the fix is real, not just a passing test

The regression test itself was initially vacuous: the fake "new release" package it builds
is copied from the current `implementation/` source, so in-process and subprocess execution
of the install step produced byte-identical output either way. Fixed by stamping a sentinel
marker into *only* the fake package's on-disk copy of `installer.GLOBAL_WRAPPER_TEMPLATE`
(never into the test process's already-imported `installer` module), making the test
genuinely falsifiable. Confirmed by reverting the fix and re-running: the test now fails
without it, and passes with it restored.

**Separately, a full manual end-to-end validation was run outside the test suite**: a real
install built from this exact released source tree, patched to report `VERSION = "1.10.7"`
(simulating the genuinely-installed IR1 state) with the exact stale pre-dispatch wrapper IR1
had, then a real, separate-process `apply_update()` call upgrading it to this newly-built
v1.10.8 package. Result: the wrapper was regenerated with correct dispatch logic, `VERSION`
correctly reports 1.10.8, config/secrets were preserved byte-for-byte, a pre-update backup
was taken, and `tixo-renewal doctor`/`status`/`readiness`/`update-check`/`policy-show`/
`--version` all dispatched correctly through the real post-upgrade wrapper.

## Testing

Full regression suite: **1417 passed, 0 failed**. Reproducible build verified (two clean
builds, identical SHA-256). Manual real-process upgrade validation (above) passed.

## Deployment status

Not deployed to `ir1` or `ir2` by this session. IR1 is on v1.10.6 with the original stale
wrapper. Upgrading directly to this release fixes it permanently — this and every future
upgrade from this point on regenerates the wrapper via a real subprocess of the
newly-downloaded code, not a cached in-process call.

**Exact upgrade command** (run on IR1, as the operator):

```bash
/usr/local/bin/tixo-renewal update-apply --yes
```

v1.10.6's wrapper already unconditionally execs `menu.py` with no dispatch logic — but
`update-apply` is one of the args that reaches `renewal_cli.py` fine via that same
unconditional exec only if the wrapper passes `$@` through, which it does not (it's a
hardcoded no-arg call in the stale version). Use the full path instead for this ONE upgrade:

```bash
PYTHONPATH="/opt/tixo-renewal/lib:/opt/tixo-renewal/bin" \
  python3 /opt/tixo-renewal/bin/renewal_cli.py update-apply --yes
```

(Verified directly against IR1's actual installed wrapper and `TIXO_RENEWAL_INSTALL_DIR` —
confirmed via read-only SSH inspection at the time this release was prepared: IR1's wrapper
hardcodes `/opt/tixo-renewal` and reports `VERSION = "1.10.6"`.) Every upgrade after this one
can simply use `tixo-renewal update-apply --yes`, since the wrapper will stay current
automatically.
