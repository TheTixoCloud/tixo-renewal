# Deployment Guide

## Architecture

The software runs **directly on each Virtualizor host**. It reads that host's own Virtualizor
database locally (same credential-reading approach as Virtualizor's own admin scripts — no
new credentials invented, no SSH, no dependency on any other server at runtime). Each host
keeps its own SQLite ledger, its own policy config, its own backups, and its own systemd
timer. There is no central server and no agent inside any customer VPS.

## Step 1 — Build the package (on your dev/build machine)

```bash
cd deploy
./build_package.sh
```

Produces `deploy/tixo-renewal-package.tar.gz` — a dependency-free bundle (Python standard
library only: `sqlite3`, `json`, `decimal`, `subprocess`, `urllib`). No pip, no venv, no
internet access required on the target host.

## Step 2 — Copy to the target Virtualizor host

```bash
scp deploy/tixo-renewal-package.tar.gz root@YOUR_HOST:/root/
ssh root@YOUR_HOST 'mkdir -p /root/renewal-package && tar -xzf /root/tixo-renewal-package.tar.gz -C /root/renewal-package'
```

## Step 3 — Install (ON the Virtualizor host)

```bash
cd /root/renewal-package
bash install.sh
```

This is idempotent and **read-only with respect to Virtualizor**: it only creates files under
`/opt/tixo-renewal` (or `$TIXO_RENEWAL_INSTALL_DIR` if set), writes default `nodes.json` /
`policy.json` (only if they don't already exist), generates every systemd unit file, and
(v1.10.3+, root only) installs+enables the 4 READ-ONLY core timers for real -- see Step 6.
It never touches the write-capable `apply` timer.

## Step 4 — Configure your real policy (ON the Virtualizor host)

Edit `/opt/tixo-renewal/renewal-config/policy.json` (see
`docs/examples/policy.example.json`). Ships with a safe default: 100 GB initial / **0 GB
monthly gift** (carry-over of purchased, unused bandwidth is the business model -- not a
recurring bonus grant; configure a nonzero gift explicitly if you want one) / unlimited
carryover / **`auto_renewal_enabled: false`** globally. Nothing is ever applied
automatically until you explicitly turn this on, at whichever level (global/host/VPS) makes
sense for you.

```bash
tixo-renewal policy-validate
tixo-renewal policy-show
```

## Step 5 — Read-only verification (ON the Virtualizor host)

```bash
tixo-renewal dry-run
tixo-renewal preview --node self --vpsid <id>
tixo-renewal status
```

Run these repeatedly, for real, against real VPS data, before trusting any number they
produce. `preview` always says `[PREVIEW ONLY, NOT APPLIED]`.

## Step 6 — Activate the read-only monitoring/inventory/hostnet/notifier timers (ON the Virtualizor host)

As of v1.10.3, `install.sh` does this automatically when run as root (Step 3) -- this step
is only needed if you set `TIXO_RENEWAL_NO_AUTO_SYSTEMD=1` during install, or are running
as a non-root user:

```bash
sudo tixo-renewal install-activate-core-timers
```

Installs and enables all 4 READ-ONLY core timers (monitor, inventory-sync, hostnet-sync,
readiness-notifier) -- none of them can call `apply`/`apply-due`, by construction. Safe to
re-run (idempotent). Verify any time with `tixo-renewal install-health-check`.

## Production write activation

This is a **separate, deliberate process** — see `docs/PRODUCTION_ACTIVATION.md`. Nothing in
steps 1–6 above creates write capability.

## Backup / rollback (ON the Virtualizor host)

```bash
tixo-renewal backup
tixo-renewal rollback --node self --backup-file <name> --yes
```

## Updating to a newer release (ON the Virtualizor host, v1.10.2+)

```bash
cd /opt/tixo-renewal
tixo-renewal update-check          # read-only
tixo-renewal update-apply --yes    # backs up first; auto-rolls-back on failure
```

See `docs/INSTALL_GUIDE.md` for the full behavior (what's preserved, what triggers a
rollback). Never run this against `ir1` or `ir2` without an operator's explicit go-ahead —
see `FUTURE_DEVELOPMENT_HANDOVER.md` for the prepared, host-specific checklist.

## Uninstall (ON the Virtualizor host)

```bash
cd /opt/tixo-renewal   # or wherever install.sh reported
tixo-renewal uninstall-preview            # read-only, shows what would happen
tixo-renewal uninstall          # keeps ledger/config
tixo-renewal uninstall --purge  # removes everything local (requires typing
                                               # 'PURGE ALL TIXOCLOUD DATA' when prompted)
```
