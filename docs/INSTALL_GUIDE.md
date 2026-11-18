# Installation, Upgrade, Repair, Reconfiguration, and Uninstall Guide

This guide covers the **core renewal engine** installer. It does not cover the optional
modules (Web Control Center, Telegram bot, relay worker, remote agent, backup scheduling)
— see `WEB_AND_TELEGRAM_SETUP.md`, `BACKUP_AND_RESTORE.md`, and `REMOTE_AND_RELAY.md` for
those.

**Verification status:** everything in this guide is verified by real, local, automated
tests (unit tests calling the functions directly, and end-to-end tests that build the real
package and run `renewal_cli.py` as a genuine subprocess against a disposable directory).
Nothing here has been run against a real production Virtualizor host.

## Fresh installation

```bash
tar -xzf tixo-renewal-package.tar.gz
cd <extracted dir>
sudo ./install.sh
```

What it does (and nothing more):
- Copies `lib/`/`bin/` into `/opt/tixo-renewal` (override with `TIXO_RENEWAL_INSTALL_DIR`).
- Creates `shadow-monitor-state/`, `renewal-config/`, `backups/`, `systemd/`.
- Writes a **safe-by-default** `policy.json` (`auto_renewal_enabled=false`) and `nodes.json`
  (`mode=local`, this host) — never overwrites either if they already exist.
- Writes every systemd unit file (core timers + every optional module's unit) into
  `systemd/` — **never copies them into `/etc/systemd/system` or enables/starts anything**.
  That is always a separate, manual, explicit step (printed at the end of install).
- Installs a global `tixo-renewal` command (launches the interactive menu) if
  `/usr/local/bin` is writable.

Nothing is enabled. Write capability (an actual renewal) remains permanently blocked until
you deliberately create `renewal-config/WRITE_APPROVED`.

## Existing-installation detection

```bash
renewal_cli.py install-detect
```

Read-only. Reports `{"existing": true/false, "has_nodes": ..., "has_policy": ...,
"db_files": [...]}` by checking for `nodes.json`/`policy.json`/any `.sqlite3` file. The
interactive menu's "Install / Initial Setup" screen runs this automatically and offers the
right path.

## Upgrade

```bash
renewal_cli.py install-upgrade
```

- Backs up config + state to a full archive **first** (printed path).
- Re-runs the same idempotent directory/default-config/schema/systemd-unit generation a
  fresh install does.
- **Never overwrites an existing `nodes.json` or `policy.json`** — your node identity,
  credentials references, and bandwidth policy survive untouched.
- Regenerates every systemd unit template (core + optional) with current defaults.

## Repair

```bash
renewal_cli.py install-repair
```

Same backup-first discipline as upgrade, plus:
- Recreates any missing directory/file/systemd-unit.
- Re-tightens any optional-module secret file (`secrets.json`, `csrf_secret.key`,
  `passphrase.key`, `users.json`) whose permissions were loosened back to `0600` — never
  widens a permission, only ever tightens one that drifted.

## Reconfiguration

Each optional module keeps its own dedicated interactive menu screen — there is no single
combined "reconfigure everything" screen:
- "Configure Telegram bot (optional)"
- "Configure Web Control Center (optional)"
- "Configure Relay Worker (optional, run on a SEPARATE host)"
- "Configure Remote Agent (optional, read-only)"
- "Configure Backup Schedule (optional)"
- "Configure monthly gift / intervals / timezone" — core engine tuning.

## Safe uninstall of ONE optional module (without touching anything else)

```bash
renewal_cli.py uninstall-module --module telegram        # keeps the stored bot token
renewal_cli.py uninstall-module --module web --purge-secrets   # also deletes web/users.json etc.
```

Valid `--module` values: `telegram`, `web`, `relay_worker`, `agent`, `backup_schedule`.
Disables the module (`enabled=False`), optionally purges its secret, removes its generated
systemd unit file. Never touches the core install, any other module, or historical data.

## Full uninstall (the whole local install)

```bash
renewal_cli.py uninstall          # removes local systemd unit files only, keeps all data
renewal_cli.py uninstall --purge  # also deletes shadow-monitor-state/ and renewal-config/
```

If any unit was ever actually installed into `/etc/systemd/system`, the command prints the
exact `systemctl disable --now ...` / `rm` commands — it never runs them for you.

## Idempotent repeated installation

Running `install` (or `install-upgrade`) any number of times in a row is always safe:
`CREATE TABLE IF NOT EXISTS` everywhere, default-config writers that no-op if the file
already exists, and systemd-unit generation that simply overwrites the (never-installed)
template files with the current defaults.

## Rollback after a failed upgrade/repair

```bash
renewal_cli.py install-rollback --backup-file <the archive upgrade/repair printed> --yes
```

- Integrity-verifies the backup archive **before** touching anything.
- Safety-copies the **current** (possibly broken) state first, so a bad rollback choice is
  never itself unrecoverable.
- Restores the backed-up databases and allowlisted config files.

## Local vs. remote mode

**Local mode is the default and the only fully-supported mode** — this engine runs ON the
Virtualizor host and reads the local Admin API config directly. "Remote mode" in this
project means the separate, optional remote-management-agent/relay-worker features (see
`REMOTE_AND_RELAY.md`) — it does **not** mean "run the core renewal engine against a
remote Virtualizor host over SSH with elevated trust"; that configuration exists in
`nodes.py` (`mode="ssh"`) from earlier project history but is not part of this release's
verified scope.

## Secret-file permissions

Every secret file this project ever writes (Virtualizor API credentials, Telegram bot
token/SOCKS5 password/relay shared secret, Web Control Center user store/CSRF key, relay
worker/remote agent shared secrets, backup encryption passphrase) is written atomically
with `0600` permissions and `load_*` functions refuse to read a file whose permissions have
been loosened. `install-repair` re-tightens any that have drifted.
