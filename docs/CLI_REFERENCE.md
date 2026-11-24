# CLI Reference (`renewal_cli.py`)

Generated from the real argparse definitions in `implementation/renewal_cli.py` (run
`python3 renewal_cli.py <command> --help` yourself to reproduce/verify this). All commands
require `TIXO_RENEWAL_INSTALL_DIR` and `PYTHONPATH=<install>/lib` set in the environment (the
installed `tixo-renewal` wrapper and the generated systemd units already do this).

Two entry points exist: `renewal_cli.py <command>` (scriptable/automatable) and the
interactive menu (`tixo-renewal`, i.e. `python3 lib/menu.py`, which wraps a subset of these
same commands behind numbered screens — see `menu.py`'s `MENU_ITEMS`).

**v1.10.6+: the installed global `tixo-renewal` command unifies both.** With no arguments
(or `--status`/`--version`/`--cli`) it opens the menu; with any other argument it dispatches
straight to `renewal_cli.py`'s real subcommands below, with `PYTHONPATH`/
`TIXO_RENEWAL_INSTALL_DIR` already set — e.g. `tixo-renewal status` is exactly
`renewal_cli.py status` with no manual environment setup needed. Two friendly aliases are
rewritten before argparse ever sees them (one implementation each, never duplicated):
`health-check` → `install-health-check`, `update` → `update-apply`.

## Install / upgrade / recovery (idempotent, config-preserving)

```
install                   Fresh install or safe re-run; writes default config only if absent.
install-detect             Read-only: fresh vs. existing install state (JSON).
install-upgrade             Backs up first, re-runs idempotent install steps + regenerates
                            systemd unit TEMPLATES (never installs/enables them).
install-repair              Recreates missing files/schemas, fixes loosened 0600 permissions.
install-rollback --backup-file F --yes
                            Restores config/state from a full backup archive (destructive by
                            design -- safety-copies current state first).
install-health-check        Read-only post-change sanity check (JSON). As of v1.10.3 this
                            ALSO verifies real systemd state (enabled/active/failed) for
                            every one of this project's own timers -- not just that config
                            files parse. Friendly alias: `health-check`.
readiness                   v1.10.6+: read-only per-VPS financial-engine readiness summary
                            (JSON) -- COLLECTING_BASELINE (normal immediately after a fresh
                            install, self-clears after the second observation, never an
                            error), MONITORING (steady state), REVIEW_REQUIRED (a genuinely
                            ambiguous pattern), RENEWAL_CANDIDATE_PENDING_REVIEW (awaiting
                            confirmation), MANUAL_ONLY_UNLIMITED (permanently excluded).
                            Distinct from install-health-check: a node can be fully healthy
                            at the service level while every VPS is still
                            COLLECTING_BASELINE. Exit 1 only for REVIEW_REQUIRED cases.
doctor                      v1.10.6+: read-only, human-readable combination of
                            install-health-check + legacy-automation detection +
                            credential presence + backup freshness + writer state -- the
                            one command to run before assuming something is broken.
logs [--limit N]            v1.10.6+: read-only recent renewal operations (from the
                            durable operations table) and recent backups, per node.
install-activate-core-timers
                            Root-only, v1.10.3+: installs and enables the 4 READ-ONLY core
                            timers (monitor, inventory-sync, hostnet-sync,
                            readiness-notifier) into real systemd -- copies the unit files,
                            runs daemon-reload, then `enable --now` on each timer. Called
                            automatically by install.sh when run as root (opt out with
                            TIXO_RENEWAL_NO_AUTO_SYSTEMD=1). Idempotent; never touches the
                            write-capable apply timer.
enable-auto-renewal [--yes] [--override-legacy-write-conflict]
                            v1.10.3+: the single, unified production-write authorization
                            step -- creates renewal-config/WRITE_APPROVED, sets
                            policy.json's auto_renewal_enabled=true, AND (if root)
                            installs+enables the write-capable virt-renewal-apply timer,
                            all together. Requires --yes or interactive confirmation.
                            v1.10.5+: refuses by default if a foreign, write-capable
                            bandwidth-automation cron job (e.g. a pre-existing
                            script-based carry-over system installed independently of
                            this project) is detected active on the host --
                            --override-legacy-write-conflict proceeds anyway, without
                            touching that job. See `install-health-check`'s
                            `legacy_bandwidth_automation` field to inspect what was found.
disable-auto-renewal         v1.10.3+: always reverses all three of the above together --
                            removes WRITE_APPROVED, sets auto_renewal_enabled=false, and
                            (if root) stops/disables/removes the apply timer. There is no
                            supported in-between state. Monitoring/historical data untouched.
update-check                Read-only: current vs. latest published GitHub release.
update-apply [--version V] [--yes] [--force]
                            Downloads, checksum-verifies, and applies a release (latest, or
                            a specific --version). Takes a full raw filesystem backup first;
                            preserves renewal-config/ and shadow-monitor-state/ untouched;
                            auto-rolls-back on any failure (bad checksum, failed download,
                            missing dependency, broken import, failed health check). Requires
                            --yes or interactive confirmation; --force applies even if the
                            target isn't newer than the installed version. Friendly alias:
                            `update` (rewritten to update-apply before argparse sees it --
                            same confirmation requirement applies either way).
uninstall-module --module {telegram,web,relay_worker,agent,backup_schedule} [--purge-secrets]
                            Disables ONE optional module without touching the core install.
uninstall-preview [--purge]  Read-only: exactly what `uninstall`/`uninstall --purge` would do
                            (which units stop, which paths are removed vs. preserved), without
                            doing it.
uninstall [--purge] [--confirm PHRASE] [--delete-backups] [--yes]
                            Standard mode: stops/disables only the app's own systemd units,
                            removes app code, preserves renewal-config/, shadow-monitor-state/,
                            and existing backups. --purge also removes config/secrets/
                            databases/ledgers (never backups/ unless --delete-backups is also
                            given) -- requires typing 'PURGE ALL TIXOCLOUD DATA' (or --confirm
                            with that exact phrase, for scripted use with --yes). Never removes
                            Virtualizor data, other VPSs, or anything not app-owned.
```

## Status / verification (read-only)

```
status                      Human-readable dashboard summary.
dry-run                     Read-only renewal-engine evaluation (what WOULD happen).
verify                      Policy/config/credential sanity checks.
hostinfo                    CPU/RAM/disk/network/temperature snapshot (JSON-ish print).
virtualizor-info [--node N] Virtualizor install detection + Admin API reachability.
inventory-status --node N   Last sync freshness, record count, coverage.
hostnet-status               Current interface, cumulative totals, persistent rate.
policy-show / policy-validate
                            Print / validate the active bandwidth policy.
```

## Backup / rollback (renewal ledger, NOT the full-system backup used for deployment)

```
backup                      Quick single-node ledger backup.
backup-full [--label L] [--encrypt] [--keep N]
                            Full archive of all known state DBs + allowlisted non-secret
                            config (see backup.py -- NEVER includes secrets.json/users.json/
                            csrf_secret.key). --encrypt uses GPG with a passphrase read from
                            the TIXO_BACKUP_PASSPHRASE environment variable, never a CLI arg.
backup-scheduled-run        Entry point for the optional backup timer.
rollback [--node N] [--backup-file F] [--yes]
                            Restores ONE node's ledger from a backup (not the full system).
```

## Credentials (interactive; never accepts a credential as a CLI argument)

```
store-credentials --node N      Interactive, non-echo prompt; writes 0600 credential file.
validate-credentials --node N   Confirms stored credentials still work against the Admin API.
diagnose-api --node N           Deeper Admin API connectivity diagnostics.
```

## Renewal (read-only preview vs. the one real write path)

```
preview --node N --vpsid V                    [PREVIEW ONLY, NOT APPLIED] -- always safe.
apply --node N [--vpsid V] [--period-key K] [--new-quota-gb G] [--execute] [--non-interactive]
                            The ONLY command that can write a quota change to Virtualizor.
                            Requires --execute explicitly; refuses without
                            renewal-config/WRITE_APPROVED present; interactive confirmation
                            unless --non-interactive (still requires --execute).
apply-due --node N [--non-interactive]         Applies every currently-due renewal for a node.
                                                Exit 3 (not 1) specifically when
                                                WRITE_APPROVED is missing -- a deliberate,
                                                by-design safety refusal, not a crash; this
                                                command's own unit declares
                                                SuccessExitStatus=3 so systemd never shows
                                                it as a failed service for that reason.
inspect-vps --node N --vpsid V                 Detailed single-VPS state for debugging.
resolve-operation --node N [--mark-resolved ID] [--resolution {APPLIED,FAILED}] [--verified-quota-gb G]
                            Manually resolves an uncertain/failed renewal operation record.
check-exhausted [--node N]                     Evaluates the exhausted-bandwidth policy now.
```

## Web / Telegram / Relay / Agent (long-running services; normally run via systemd)

```
web-run [--max-iterations N]            Starts the Web Control Center (blocks; see
                                        web_server.py). --max-iterations bounds it for
                                        manual testing only -- omit for the real deployment.
telegram-bot-run [--max-iterations N]   Starts the Telegram bot long-polling loop.
relay-worker-run [--max-iterations N]   Starts the relay worker (runs on a SEPARATE host).
agent-run [--max-iterations N]          Starts the read-only remote management agent.
install-ui [--yes]                      Installs the optional Textual TUI control center.
send-report [--period LABEL]            Sends a manual report through the configured
                                        Telegram transport right now.
```

## Inventory / network interface configuration

```
inventory-sync --node N                 Forces an immediate inventory sync (normally the
                                        inventory-sync timer does this automatically).
                                        Exits 0 with status=NOT_CONFIGURED (not a failure)
                                        if credentials haven't been stored yet -- this is
                                        the ExecStart of a timer now auto-enabled on every
                                        fresh install, so "not configured yet" must never
                                        look like a crash.
hostnet-suggest                         Lists candidate physical/bridge interfaces.
hostnet-configure --interface IFACE [--yes]
                                        Confirms which interface the hostnet sampler reads
                                        (refuses a detected-virtual interface).
hostnet-sync                            Forces one immediate hostnet sample (normally the
                                        hostnet-sync timer does this automatically).
```

## Exit codes

`0` success (including a deliberate, by-design "not configured yet" state such as
`inventory-sync` before credentials are stored), `1` failure/blocked (e.g.
`install-health-check` with problems, a rejected `apply` without `--execute`), `3`
specifically for `apply-due`'s "WRITE_APPROVED missing" refusal (see above -- kept distinct
from `1` so systemd's `SuccessExitStatus=3` on that one unit can tell a deliberate safety
block apart from a real crash). Commands never raise an uncaught traceback for an expected
failure mode (missing config, invalid credentials, policy validation errors) — always a
clean message + the appropriate exit code.
