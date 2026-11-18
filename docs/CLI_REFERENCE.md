# CLI Reference (`renewal_cli.py`)

Generated from the real argparse definitions in `implementation/renewal_cli.py` (run
`python3 renewal_cli.py <command> --help` yourself to reproduce/verify this). All commands
require `TIXO_RENEWAL_INSTALL_DIR` and `PYTHONPATH=<install>/lib` set in the environment (the
installed `tixo-renewal` wrapper and the generated systemd units already do this).

Two entry points exist: `renewal_cli.py <command>` (scriptable/automatable) and the
interactive menu (`tixo-renewal`, i.e. `python3 lib/menu.py`, which wraps a subset of these
same commands behind numbered screens — see `menu.py`'s `MENU_ITEMS`).

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
install-health-check        Read-only post-change sanity check (JSON).
uninstall-module --module {telegram,web,relay_worker,agent,backup_schedule} [--purge-secrets]
                            Disables ONE optional module without touching the core install.
uninstall [--purge]         Removes locally generated systemd unit files (prints the manual
                            `systemctl disable` command for any installed into
                            /etc/systemd/system -- never does that automatically).
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
hostnet-suggest                         Lists candidate physical/bridge interfaces.
hostnet-configure --interface IFACE [--yes]
                                        Confirms which interface the hostnet sampler reads
                                        (refuses a detected-virtual interface).
hostnet-sync                            Forces one immediate hostnet sample (normally the
                                        hostnet-sync timer does this automatically).
```

## Exit codes

`0` success, `1` failure/blocked (e.g. `install-health-check` with problems, a rejected
`apply` without `--execute`). Commands never raise an uncaught traceback for an expected
failure mode (missing config, invalid credentials, policy validation errors) — always a
clean message + nonzero exit.
