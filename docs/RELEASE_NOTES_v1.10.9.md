# TixoCloud Control Center v1.10.9 — Fixed the Actual Bootstrap Problem

`renewal_cli.VERSION = "1.10.9"`. v1.10.7 and v1.10.8 both shipped real fixes for the
stale-global-wrapper regression, and both were incomplete in the same way: **confirmed on
IR1**, a real `v1.10.6 -> v1.10.8` upgrade completed ("Update applied successfully: v1.10.6
-> v1.10.8"), `VERSION` correctly read `1.10.8`, and `tixo-renewal doctor` **still** opened
the interactive menu instead of the Doctor report.

## Root cause: the bootstrap problem

v1.10.7's fix (`write_global_wrapper`, called from `cmd_install`) and v1.10.8's fix (run the
install step as a subprocess of the newly-downloaded code) both placed the repair logic
inside `updater.py`/`installer.py` — **the new release's own code**. But an upgrade is
executed by whichever updater code is *already running* at the moment the operator invokes
`update-apply`. A real v1.10.6 installation upgrading to v1.10.8 runs **v1.10.6's own
`updater.py`**, in-process — code that has no knowledge v1.10.8's `updater.py` was ever
changed, because it was written and published before that fix existed. No fix placed inside
a new release's Python logic can ever repair the upgrade that installs it for the first
time — only every upgrade *after* that one.

**This is why the previous "end-to-end validation" (v1.10.8's release notes) was invalid.**
It simulated "the old version" by patching a `VERSION` string into a copy of current
source — which structurally represents "current code calling current code" regardless of
the string, and could never expose a bug that only exists when *genuinely old* code is
executing. This release's validation was redone using the actual, previously-published
v1.10.6 release artifact, downloaded fresh and checksum-verified, running its own real
(unmodified) `updater.py` to drive the upgrade.

## Fix

The repair now lives in **`menu.py`**, not the updater. The one thing that has *always*
worked correctly on every upgrade, regardless of which version's (possibly completely
broken) updater performed it, is that `lib/`+`bin/` get wholesale file-swapped to the new
release's code — `VERSION` has always ended up correct. So `menu.py` — the file an
arbitrarily stale wrapper *always* unconditionally execs for every argument, no matter how
old or broken it is — is current code the instant it runs, independent of the updater
entirely. `menu.py`'s `main()` now:

1. Regenerates the global wrapper on **every single invocation**, best-effort
   (`_self_heal_global_wrapper()`).
2. Dispatches any real subcommand (anything other than the three menu-only flags) straight
   to `renewal_cli.main()` instead of silently opening the interactive menu.

Neither depends on the updater's own correctness, or on any particular upgrade having gone
through a code path that knows to call `write_global_wrapper()`.

## Also: refuse to claim success

`cmd_install()` now actually **runs** the wrapper it just wrote
(`installer.verify_wrapper_dispatch`, via `--version` and the real `install-detect`
subcommand) and refuses to report success if dispatch doesn't genuinely work end to end.
This immediately caught a real latent bug in three of this project's own deploy-package
tests, which called `renewal_cli.py install` with code and data split across two separate
directories — a layout the wrapper can never work with, and one `install.sh` never actually
produces. Fixed those tests to mirror `install.sh`'s real single-directory layout, and
discovered (while fixing them) that none of this test file's subprocess-based tests had ever
overridden `TIXO_RENEWAL_WRAPPER_PATH` — meaning the suite had been silently writing to
the real host's `/usr/local/bin/tixo-renewal` on every run. Fixed.

## New regression test, verified genuinely falsifiable

`test_menu_self_heals_an_arbitrarily_stale_wrapper_regardless_of_which_updater_ran`
hand-constructs the exact pre-v1.10.7 wrapper form (confirmed via read-only SSH to be
precisely what is still on IR1's disk right now) and proves a single command through it
both reaches the real subcommand and self-heals the wrapper for every subsequent call.
Confirmed by temporarily disabling the fix and re-running: fails without it, passes with it
restored.

## Real end-to-end validation (not simulated)

1. Downloaded the actual, previously-published **v1.10.6** release artifact from GitHub,
   checksum-verified.
2. Installed it for real (its own code, unmodified) into an isolated directory, confirmed
   `VERSION == "1.10.6"`.
3. Hand-constructed the exact stale, pre-dispatch wrapper IR1 has (v1.10.6 predates
   `write_global_wrapper` entirely — back then `install.sh`'s bash heredoc wrote it).
4. Built this release (v1.10.9) locally, reproducible (two clean builds, identical SHA-256).
5. Ran **v1.10.6's own, genuinely unmodified `updater.py`** — not a copy, not a simulation —
   calling its real `apply_update()`, pointed at the local v1.10.9 build via its existing
   injectable `fetch_fn`/`download_fn` parameters (the only thing supplied externally,
   since v1.10.9 is not yet published and the real GitHub API has nothing to fetch).
6. Confirmed the upgrade reported success and `VERSION` became `"1.10.9"`.
7. Confirmed the wrapper was, as expected, **still stale** immediately after — this is the
   exact IR1 symptom, faithfully reproduced with real code, not assumed.
8. Ran `tixo-renewal doctor` through that still-stale wrapper: it produced the real Doctor
   report immediately — not the interactive menu — because `menu.py` (now v1.10.9's code)
   dispatched it directly.
9. Confirmed the wrapper was now self-healed on disk, and every other global command
   (`status`, `readiness`, `policy-show`, `--version`) worked correctly through it with no
   further operator action.
10. Confirmed config/secrets preserved, pre-update backup taken.

## Testing

Full regression suite: **1418 passed, 0 failed**. Reproducible build verified.

## Deployment status

Not deployed to `ir1` or `ir2` by this session. IR1 is on v1.10.8 (confirmed via read-only
SSH) with the original stale wrapper, still the literal first-install form — predating
dispatch logic entirely. Upgrading to v1.10.9 will fix it immediately: the upgrade itself
may still leave the wrapper stale for one moment (depending on exactly how the currently-
running v1.10.8 updater behaves), but the very next command run afterward — including
`doctor` itself — will now self-heal and dispatch correctly, with no second upgrade and no
operator intervention required.

**Exact upgrade command** (run on IR1, as the operator — full path required for this one
upgrade, since the currently-installed wrapper does not forward arguments):

```bash
PYTHONPATH="/opt/tixo-renewal/lib:/opt/tixo-renewal/bin" python3 /opt/tixo-renewal/bin/renewal_cli.py update-apply --yes
```

After this single upgrade, `tixo-renewal doctor` (and every other command) will work
immediately, and the wrapper will stay self-healing on every future upgrade regardless of
what that upgrade's own updater code does or does not know how to do.
