# TixoCloud Control Center — Future Development Handover

**Status at handover: v1.10.0 in production on ir1 (web/Telegram live and verified
working); v1.10.2 is the latest tagged GitHub release (real in-app updater, auto-launch
install menu, two-mode uninstaller — see `docs/RELEASE_NOTES_v1.10.2.md`), not yet deployed
to ir1 or ir2. ir2's first install is still pending — it previously failed with the exact
bug v1.10.1 fixed (see `docs/KNOWN_LIMITATIONS.md` §1). Production (ir1) code was, as of the
v1.10.1 handover, current for every file except `renewal_cli.VERSION` itself (ir1 still
reports v1.10.0). **No code on ir1 or ir2 has been touched as part of the v1.10.2 work** —
both remain exactly as the v1.10.1 handover left them; see §17 for the prepared (not
executed) upgrade procedure. See `docs/DEVELOPMENT_PROGRESS.md` for the full chronological
history and `docs/KNOWN_LIMITATIONS.md` for honest outstanding issues.**

## 1. What TixoCloud currently does

An on-host bandwidth renewal/monitoring system for a Virtualizor hypervisor: tracks each
VPS's bandwidth quota/usage, can (when explicitly write-approved) apply monthly renewals,
monitors host-level network traffic, and surfaces all of this through three interchangeable
surfaces — an interactive CLI menu, a Web Control Center, and a Telegram bot — all built on
one shared, read-only backend service layer (`control_center_api.py`). The write path
(actually changing a VPS's quota in Virtualizor) is deliberately narrow, CLI-only, and
multiply-gated (see `docs/ARCHITECTURE.md` → "Write path").

## 2. Production architecture

See `docs/ARCHITECTURE.md` for the full diagram and process table. Short version: one
process per concern, mostly systemd timers (read-only by default), zero third-party Python
dependencies, one SQLite database per concern under `shadow-monitor-state/`, JSON config
under `renewal-config/`.

## 3. Repository structure

```
implementation/        All application source (flat, one module per concern)
  static/               Web branding assets + live-bandwidth.js (v1.10.0+)
deploy/                 install.sh, uninstall.sh, build_package.sh, bootstrap.sh
tests/                  Full pytest suite (1300+ tests)
docs/                   This handover + architecture/API/CLI/schema reference +
                        release notes + deployment/backup/security docs
systemd/, deploy/systemd-notifier/   Example generated unit files (git-tracked references;
                        the real ones are generated fresh by `renewal_cli.py install`)
```

## 4. Development environment setup

```bash
git clone https://github.com/TheTixoCloud/tixo-renewal.git
cd tixo-renewal
python3 -m pip install pytest   # the ONLY external dependency, dev-only (never shipped)
```
No venv/requirements.txt needed beyond `pytest` itself — the application has zero runtime
dependencies (stdlib only). Python 3.12 was used throughout this engagement; the code avoids
anything newer than broadly-available 3.10+ stdlib features.

## 5. Running the full test suite

```bash
cd tixo-renewal
python3 -m pytest tests/ -q
```
Expect ~1308 passed (grows over time — check the actual count, don't hardcode it elsewhere).
A handful of tests shell out to `node` (for `tests/test_live_bandwidth_js.py`) and are
auto-skipped if Node isn't installed — install `node` to get that coverage.

## 6. Building a reproducible release

```bash
cd deploy
./build_package.sh
```
Produces `deploy/tixo-renewal-package.tar.gz` + `deploy/SHA256SUMS`. Verify
reproducibility yourself: run it twice from a clean tree, diff the two tarball checksums —
they must match exactly (fixed mtimes/ownership in the tar, `gzip -n`). See
`tests/test_deploy_package.py`.

Publishing (tag + GitHub release) is a separate, deliberate step:
```bash
git tag -a vX.Y.Z -m "..."
git push origin vX.Y.Z
gh release create vX.Y.Z deploy/tixo-renewal-package.tar.gz deploy/SHA256SUMS \
  --title "..." --notes-file docs/RELEASE_NOTES_vX.Y.Z.md
```

## 7. Deploying safely

**Fresh host, one-command install** (current pinned tag — verify this still matches
`renewal_cli.VERSION` before trusting it; `test_bootstrap_default_tag_matches_current_version`
and `test_readme_install_command_matches_current_version` both guard against this drifting
again, but check anyway):
```bash
curl -fsSL https://raw.githubusercontent.com/TheTixoCloud/tixo-renewal/v1.10.2/deploy/bootstrap.sh | sudo bash
```

Full procedure in `docs/DEPLOYMENT.md`. Short version for an **existing** install:
1. `scp` the built tarball + its sha256 to the target host; verify the checksum matches.
2. Extract to a staging directory; verify the staged `renewal_cli.VERSION`.
3. **Take a fresh backup first** (see §8) — `install.sh`/`install-upgrade` also takes their
   own automatic backup, but don't rely on that alone for anything you can't afford to lose.
4. Run `install.sh` from the staging directory — as of v1.10.1 this completes cleanly
   end-to-end (the `ModuleNotFoundError` partial-install bug from v1.10.0 and earlier is
   fixed; see `docs/KNOWN_LIMITATIONS.md` §1). Never overwrites existing
   `policy.json`/`nodes.json`/secrets. As of v1.10.2, a successful install run from an
   interactive terminal (including piped `curl | bash`) automatically opens the management
   menu afterward; a failed install never does.
5. Run `install-health-check`; confirm it reports `{"ok": true}`.
6. If a Web/Telegram service was running, restart ONLY that service
   (`systemctl restart virt-renewal-web.service` / `...-telegram-bot.service`) — the five
   renewal timers never need restarting for an application code change (they re-exec the
   script fresh on every timer firing).
7. Verify the five timers are still enabled/active and their unit-file checksums are
   unchanged (a code upgrade should never need to touch `/etc/systemd/system/*`).

## 8. Restoring a backup

Two different backup formats exist — know which one you have (see
`docs/ARCHITECTURE.md` → "Backup formats", `docs/BACKUP_AND_RESTORE.md`):
- **`backup-full` / Web `/backups` archive** (databases + non-secret config only): restore
  via `renewal_cli.py install-rollback --backup-file <name> --yes` (safety-copies current
  state first) or `rollback --node N --backup-file F --yes` for a single node's ledger.
- **A full raw filesystem tar** (everything, including secrets — the kind used for this
  project's own pre-deployment safety backups): `tar -xzf <backup>.tar.gz -C /` after first
  moving the current install directory aside (`mv /opt/tixo-renewal /opt/tixo-renewal.bad`),
  never a blind in-place overwrite. Always test-extract into an isolated directory first to
  confirm the archive is actually readable before touching anything live.

## 9. Using the CLI

See `docs/CLI_REFERENCE.md` (generated from the real `--help` output) for every command.
Interactive menu: `tixo-renewal` (installed globally by `install.sh`) or
`python3 lib/menu.py` directly. Non-interactive: `renewal_cli.py <command> [args]`.

## 10. How Web and Telegram authentication work

**Web**: session-cookie-based (`web_auth.py`), bcrypt-style password hashing, 10
attempts/5min login rate limit, CSRF token required on every mutating `POST`
(`web_auth.derive_csrf_token`, HMAC-derived from the session), `admin` vs `viewer` role gate
on every mutation. See `docs/WEB_API_REFERENCE.md`.

**Telegram**: numeric Telegram user ID must appear in `settings.admin_ids` or
`settings.viewer_ids` (`security.is_authorized_telegram_id`) — **never** username-based.
Every command is currently read-only; `admin`/`viewer` are treated identically today (the
distinction exists in the data model for a future mutating command). Per-user rate limiting
via `security.check_rate_limit` runs before any command logic.

## 11. How live bandwidth monitoring works

See `docs/ARCHITECTURE.md` → "Live bandwidth monitoring" and
`docs/RELEASE_NOTES_v1.10.0.md` for the full design writeup and the production
"suspicious rate values" investigation (they were real — confirmed via `ethtool`, not a
bug). Short version: `static/live-bandwidth.js` polls `GET /api/live` (one cheap
`/sys/class/net` read server-side) every ~4s and computes the rate **client-side** from the
delta between consecutive reads. No new backend process, bounded 300-sample buffer, honest
counter-reset detection, Mbps/Gbps/MB/s/GB/s unit selector.

## 12. How renewal timers operate

Five systemd timers, all read-only except the (not-auto-installed) apply timer:
`virt-renewal-monitor` (30 min, `dry-run` only), `virt-renewal-inventory-sync`,
`virt-renewal-hostnet-sync` (5 min), `virt-renewal-readiness-notifier`, and
`virt-renewal-apply` (generated for review only, never copied/enabled automatically — see
`docs/ARCHITECTURE.md`). Changing their cadence requires regenerating the unit templates via
`write_systemd_units()` (interval env vars at install time) and re-copying them to
`/etc/systemd/system/` yourself — never edit the live unit files in place and expect them to
survive the next `install-upgrade` (it regenerates the templates, but still never auto-copies
over `/etc/systemd/system/`).

## 13. How to avoid modifying customer VPSs accidentally

- The ONLY code path that can write a quota change is `renewal_cli.py apply --execute`,
  gated behind `renewal-config/WRITE_APPROVED` existing (a file you create manually, once,
  deliberately) plus interactive confirmation (or `--non-interactive`, which still requires
  `--execute`).
- Every other command — including everything the Web UI and Telegram bot can trigger — is
  read-only toward Virtualizor and customer VPS state by construction (see
  `docs/ARCHITECTURE.md` → "Write path"; `writer.py` is the only module that calls
  Virtualizor's write API, and nothing outside the `apply`/`apply-due` CLI commands imports
  it).
- `hostnet-configure` changes which host interface is *monitored* — it never touches
  interface/network configuration itself.
- When testing or demoing, always use a disposable preview (fresh `config_dir`/`state_dir`,
  a temporary diagnostic Web account deleted afterward) rather than pointing any tool at
  production state you don't intend to change — this was the pattern used throughout this
  engagement's own validation work.

## 14. How to resume work with Claude Code

- Read `docs/DEVELOPMENT_PROGRESS.md` first — it's the chronological record of every
  release's actual changes, written as the work happened, not after the fact.
- Read `docs/KNOWN_LIMITATIONS.md` for what's genuinely unfinished or rough.
- This project's established conventions (worth telling a fresh Claude Code session
  explicitly, since they're not all self-evident from the code alone): zero third-party
  dependencies ever; one shared backend layer (`control_center_api.py`) for all three
  surfaces, never re-derive business logic in Web/Telegram/CLI separately; every new
  SQLite table uses `CREATE TABLE IF NOT EXISTS` (never a destructive migration); every
  traffic/byte figure goes through `traffic_format.py`, never hand-rolled formatting; secrets
  are never read, printed, or requested in chat — operator enters them directly on the
  production host via the existing non-echo CLI prompts; production SSH access (if granted)
  always starts with independently verifying the host's SSH key fingerprint against an
  operator-confirmed value before the first connection.
- Run the full test suite before and after any change; this project treats "tests pass" as
  necessary but not sufficient — also actually render/exercise the thing you changed (curl
  the real routes, run the real CLI command) before calling something done.

## 15. Requested future Web dashboard work (NOT implemented yet — scope for next session)

The operator has asked for fleet-wide bandwidth totals on the Web dashboard, to match what
the CLI already computes via `analytics.compute_bandwidth_analytics()` but the Web UI
doesn't yet surface. Concretely:

- **Total VPS traffic quota across the fleet** — sum of `quota_gb` across all VPSs with a
  known, limited (non-unlimited) quota. The backend field already exists:
  `limited_quota_sum_gb`.
- **Total traffic consumed across all VPSs** — `total_used_all_vps_gb`, with its own
  coverage fraction (`total_used_all_vps_coverage`, e.g. `"58/60"` if 2 VPS records are
  missing usage data) — **must be displayed alongside the coverage fraction, never as a bare
  number that implies full coverage when it isn't**, consistent with this project's existing
  "never imply more completeness than is real" discipline (see `traffic_analytics.py`'s
  `coverage_status` pattern, which the Overview/Traffic Analytics pages already use for
  historical periods — reuse that same visual language here).
- **Total remaining quota** — `limited_remaining_sum_gb` (limited-fleet quota minus
  limited-fleet used; unlimited VPSs are correctly excluded from this sum already, per
  `analytics.py`'s documented rule that mixing them in would silently misrepresent both).
- **Fleet-wide quota usage percentage** — `limited_used_sum_gb / limited_quota_sum_gb * 100`,
  computed the same way the CLI's dashboard already does it (do not re-derive independently —
  call `analytics.compute_bandwidth_analytics()` and use its fields directly).
- **Per-node and per-VPS breakdowns** — the data model already supports multiple nodes
  (`nodes.json`); today's Web Inventory page implicitly assumes `node_id="self"`. A
  multi-node fleet view would need a node selector/grouping, not a new data layer.
- **Unlimited-quota accounting** — already computed separately
  (`unlimited_vps_count`, `unlimited_used_sum_gb`, `unlimited_used_sum_coverage`) and must
  stay visually distinct from the limited-fleet numbers, never summed together (an
  "unlimited" VPS has no quota ceiling to sum against — this project's `analytics.py`
  docstring is explicit that conflating them would misrepresent both).
- **Coverage and freshness indicators** — every fleet-wide figure above has a corresponding
  `*_coverage` field already computed; surface it (e.g. a small "(58/60 VPS reporting)"
  annotation) rather than silently dropping missing records from the sum without saying so.
  Also surface `inventory.last_sync_info()`'s freshness classification (already used by
  `isync.classify_freshness` in the CLI's inventory view) so an operator can tell whether
  these fleet totals are based on a recent sync or a stale one.
- **Consistency with CLI calculations** — `control_center_api.get_vps_inventory_overview()`
  **already** calls `analytics.compute_bandwidth_analytics()` and returns its full output as
  `overview["fleet"]` (the Web Inventory page already consumes part of this dict today —
  `fleet["total_vps"]`, `running_count`/`stopped_count`/`suspended_count` — just not yet the
  quota-sum fields above). No new backend plumbing is needed; this is purely a Web
  page/template task to render fields that are already present in the response. Do not
  re-derive any of this math independently in `web_server.py` — read it straight from
  `overview["fleet"]`, exactly like the existing fields do.

Suggested placement: a new KPI row on the existing `/inventory` page (above the per-VPS
table), reusing the `.kpi-grid`/`.kpi`/`.gauge` CSS classes already established by the
Overview page redesign — no new design system needed, just new data wired into the existing
components.

## 16. Closeout references

- `TixoCloud-Final-Development-Archive.zip` — sanitized source + full git history + all
  documentation (this file included) — see the accompanying closeout report for its exact
  path/checksum. **This v1.10.2 closeout deliberately does not include a production
  recovery backup** — none was requested or created; see §17 for how to apply this release
  to ir1/ir2 safely when the operator chooses to.

## 17. Prepared (not executed) ir1/ir2 upgrade procedure for v1.10.2

Neither host was touched while producing this release. The following is a checklist for an
operator to run manually, not something this session executed.

**Important nuance confirmed by real testing during this release (see the closeout
report):** `update-check`/`update-apply` are themselves *new in v1.10.2* — a host still
running v1.10.0 or v1.10.1 does not have these commands yet, so it cannot "self-update" to
v1.10.2 using them. The first hop to v1.10.2 on each host must go through the same
bootstrap/`install.sh` path used for a fresh install (which is idempotent and
config-preserving — see `docs/INSTALL_GUIDE.md` → Upgrade). Only *after* a host is running
v1.10.2 do `update-check`/`update-apply` become available for the *next* release.

**ir1** (v1.10.0 code on disk, Web + Telegram + TLS + 5 systemd timers + a live SOCKS5 patch
and existing config/ledger, per the v1.10.1 handover):
1. Preflight: `tixo-renewal backup` (fresh app-level backup, in addition to whatever infra
   snapshot ir1's owner normally takes), confirm free disk space, confirm
   `systemctl list-timers` shows the expected 5 owned timers before starting.
2. Update command (from the existing install directory, as root or with
   `TIXO_RENEWAL_INSTALL_DIR` set to ir1's real install path):
   ```
   curl -fsSL https://raw.githubusercontent.com/TheTixoCloud/tixo-renewal/v1.10.2/deploy/bootstrap.sh | sudo bash
   ```
   This re-extracts the v1.10.2 package and runs `install.sh` against the *existing* install
   directory — idempotent, never overwrites `policy.json`/`nodes.json`/secrets. (Verified
   during this release: a real v1.10.0-shaped install directory, with its original
   `policy.json`/`nodes.json`/ledger content, upgraded cleanly to v1.10.2 via this exact
   mechanism — see the closeout report.)
3. Post-update verification: `tixo-renewal --status`, confirm `VERSION` reports `1.10.2`;
   confirm Web (`curl -s localhost:<port>/api/health` or equivalent) and the Telegram bot
   both respond; confirm `systemctl list-timers` still shows the same 5 timers, still
   enabled; confirm `policy.json`/`nodes.json`/secrets/the SOCKS5 patch are byte-identical to
   pre-update (diff against the pre-update backup).
4. Rollback (only if step 3 fails): restore from the step-1 `tixo-renewal backup`, or from
   whatever infra-level snapshot was taken beforehand, then restart the owned services.
5. **From this point on** (ir1 now running v1.10.2), future releases beyond v1.10.2 can use
   `tixo-renewal update-check` / `tixo-renewal update-apply` directly — which take their own
   full filesystem backup automatically and auto-roll-back on a failed health check (verified
   end-to-end during this release, including a genuine induced-failure rollback — see the
   closeout report).

**ir2** (existing v1.10.1 install, independent Virtualizor environment, no shared
credentials/ledger with ir1):
1. Preflight: same as ir1 step 1, scoped to ir2's own install directory and timers.
2. Update command: identical to ir1 step 2 (the bootstrap one-liner), run against ir2's own
   install. (Also verified during this release against a real v1.10.1 install directory —
   see the closeout report.)
3. Post-update verification: identical to ir1 step 3, scoped to ir2's own services/timers/
   config. ir2 has no Web/Telegram services active per the v1.10.1 handover — skip those
   checks if still true, but re-verify current state first rather than assuming it hasn't
   changed.
4. Rollback: identical to ir1 step 4.
5. From this point on: identical to ir1 step 5.

Neither procedure above has been run. Do not run it without the operator's explicit
go-ahead, and never run both hosts' updates simultaneously without first confirming each
independently.
