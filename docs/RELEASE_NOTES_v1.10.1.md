# TixoCloud Control Center v1.10.1 — Installer & Release-Process Patch

`renewal_cli.VERSION = "1.10.1"`. A narrowly scoped patch fixing two release/installer
defects discovered on a second production hypervisor (ir2) and during a release-process
audit. No application behavior changes beyond the installer script itself.

## What's in this release (since v1.10.0)

- **Fixed `deploy/install.sh`'s installed-version detection crash.** Reproduced exactly:
  running `install.sh` from the published v1.10.0 release artifact against a fresh host
  crashes with `ModuleNotFoundError: No module named 'renewal_cli'` immediately after
  Virtualizor detection, because the one-line version check
  (`PYTHONPATH="$INSTALL_DIR/lib" python3 -c 'import renewal_cli'`) never included
  `$INSTALL_DIR/bin`, where `renewal_cli.py` actually lives. The application code copy
  itself (`cp -f lib/*.py`, `cp -f bin/*.py`) completes successfully *before* this line, so
  the result is a genuinely **partial install**: code present, but the systemd unit
  templates are never generated and the global `tixo-renewal` wrapper command is never
  (re)created — explaining why the CLI could still be launched directly (by invoking the
  script file, which doesn't hit this code path) even though the installer itself had
  failed. This fix was actually written during the v1.10.0 engagement (commit `4c5569b`,
  applied in-place to `ir1` without a version bump) but never made it into a tagged release
  until now — `ir2` was the first host to run the *published* v1.10.0 artifact's `install.sh`
  and hit the still-broken version.
- **Fixed `install.sh` never copying the new `lib/static/` directory** (branding assets,
  `live-bandwidth.js`, introduced in v1.10.0) — same commit as above, also not previously in
  a tagged release.
- **Fixed `README.md`'s one-command install instructions**, found pinned to the `v1.1.0`
  release tag — nine releases stale. `deploy/bootstrap.sh`'s own default tag has a dedicated
  regression test (`test_bootstrap_default_tag_matches_current_version`) that has correctly
  caught drift on every release since it was added; no equivalent test existed for
  `README.md`, so this went unnoticed for a long time. Both the instructions and a new test
  (`test_readme_install_command_matches_current_version`) are fixed in this release.

## New regression tests (3)

- `test_install_sh_fresh_install_succeeds_end_to_end` — runs the **actual `install.sh`
  script** as a real subprocess against the real built package (not `renewal_cli.py install`
  directly, which every pre-existing install-focused test called instead, and which is
  exactly why this bug was never caught). Asserts exit code 0, no traceback, code + static
  assets + all systemd unit templates present.
- `test_install_sh_second_run_recovers_and_stays_idempotent` — simulates the exact ir2
  partial-install state (code present, systemd templates missing) and confirms a second
  `install.sh` run completes cleanly and never overwrites an operator's already-edited
  `policy.json`.
- `test_readme_install_command_matches_current_version` — fails loudly if `README.md`'s
  pinned install tag ever drifts from `renewal_cli.VERSION` again.

## Scope discipline

Pure installer/release-process fix + tests + documentation. No `implementation/` runtime
module changed. No database schema change. No systemd timer, Web UI, or Telegram behavior
change.

## Testing

Full regression suite: **1311 passed, 0 failed**. Reproducible build verified. The exact
ir2 failure was reproduced against the real published v1.10.0 artifact before fixing, and
re-verified fixed against the new v1.10.1 build.

## Deployment status

Not deployed to `ir1` or `ir2` as part of this release — this closes the installer defect
for **future** installs/upgrades (including `ir2`'s still-pending first install). `ir1`
already has the equivalent fix applied in-place since the v1.10.0 engagement (see
`docs/KNOWN_LIMITATIONS.md` §1–2, now resolved by this release).
