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
  `systemd/`.
- **(v1.10.3+, root only)** Automatically installs and enables the 4 READ-ONLY core timers
  (monitor, inventory-sync, hostnet-sync, readiness-notifier) into real systemd: copies
  their unit files into `/etc/systemd/system`, runs `daemon-reload`, then
  `enable --now` each one. None of these four can write to Virtualizor by construction.
  Opt out with `TIXO_RENEWAL_NO_AUTO_SYSTEMD=1` for full manual control (or run it later
  yourself: `sudo renewal_cli.py install-activate-core-timers`, safe to re-run). Skipped
  automatically (never fatal) when not running as root.
- The write-capable `apply` timer is **never** touched by `install.sh`, under any
  circumstance — see "Automatic renewal and production-write authorization" below.
- Installs a global `tixo-renewal` command (launches the interactive menu) if
  `/usr/local/bin` is writable.
- **(v1.10.2+)** If `install.sh` was run from a genuinely interactive terminal — including a
  piped `curl | bash` one-liner, via explicit `/dev/tty` redirection — a *successful* install
  automatically opens the `tixo-renewal` management menu afterward. A failed install never
  does this. Set `TIXO_RENEWAL_NO_AUTO_MENU=1` beforehand to suppress it for a scripted or
  CI install.

Write capability (an actual renewal) remains permanently blocked until you deliberately
authorize it -- see the next section.

## Automatic renewal and production-write authorization (v1.10.3+)

Before v1.10.3, "enabling automatic renewal" meant three separate, undocumented manual
steps: creating `renewal-config/WRITE_APPROVED` by hand, editing `policy.json`'s
`auto_renewal_enabled` field, and separately installing the write-capable `apply` timer.
These are now ONE confirmed action:

```bash
renewal_cli.py enable-auto-renewal          # interactive confirmation
renewal_cli.py enable-auto-renewal --yes    # scripted use
```

This creates `WRITE_APPROVED`, sets `auto_renewal_enabled=true`, and (if run as root)
installs+enables `virt-renewal-apply.timer` -- all together, so there is never a partial
state where one of the three is true and the others aren't. The interactive menu's "Enable
automatic renewal" screen does exactly the same thing, with an explanation printed first.

To reverse it -- always all together, never partially:

```bash
renewal_cli.py disable-auto-renewal
```

This removes `WRITE_APPROVED`, sets `auto_renewal_enabled=false`, and (if root)
stops/disables/removes the apply timer. Monitoring, inventory sync, and all historical
ledger data are explicitly untouched.

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

## Checking for and applying updates (v1.10.2+)

```bash
renewal_cli.py update-check              # read-only: current vs. latest GitHub release
renewal_cli.py update-apply              # interactive confirmation required
renewal_cli.py update-apply --yes        # scripted use
renewal_cli.py update-apply --version 1.10.2 --yes   # a specific version instead of latest
```

`update-apply` re-fetches the target release, downloads and checksum-verifies the package
**before touching anything**, takes a full raw filesystem backup (printed path — this is the
complete-filesystem format, including secrets, distinct from `backup-full`'s secret-excluding
format), swaps in the new code, re-runs the idempotent install step, and runs a health
check. `renewal-config/` and `shadow-monitor-state/` are never touched. On any failure at
any stage, the previous code is restored exactly and the command exits nonzero with the
failing stage named — it never leaves the install half-updated. The interactive menu's
"Check for updates" screen wraps this same flow and reopens the (now-updated) menu on
success.

## Full uninstall (the whole local install)

```bash
renewal_cli.py uninstall-preview               # read-only: shows exactly what would happen
renewal_cli.py uninstall-preview --purge       # same, for purge mode
renewal_cli.py uninstall                       # standard: removes app code + app-owned
                                                # systemd units; KEEPS renewal-config/,
                                                # shadow-monitor-state/, and backups/
renewal_cli.py uninstall --purge               # full purge: ALSO removes config/secrets/
                                                # databases/ledgers (not backups/ unless
                                                # --delete-backups is also given) -- requires
                                                # typing 'PURGE ALL TIXOCLOUD DATA' when
                                                # prompted (or --confirm 'PURGE ALL TIXOCLOUD
                                                # DATA' --yes for scripted use)
```

Both modes only stop/disable/remove systemd units and files this project itself owns (a
fixed allowlist, matched by name AND by checking for a known signature file inside each
directory before deleting it — never by directory name alone, and never a glob/wildcard
match) — an unrelated file sitting in the install directory, or any Virtualizor/VPS data,
is never touched by either mode. The interactive menu's "Uninstall" screen wraps this same
flow and exits the process cleanly afterward (both modes delete the menu's own source file).

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
