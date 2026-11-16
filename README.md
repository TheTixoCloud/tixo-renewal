# Tixo Renewal

A host-local, policy-driven bandwidth carryover/renewal system for Virtualizor KVM VPS hosts.
Runs directly on each Virtualizor master (no agent inside customer VPSs, no runtime
dependency on any external server), with per-host and per-VPS configurable policies, a
durable SQLite ledger, and production writes disabled until an operator explicitly approves
them.

## One-command install (Ubuntu 24.04 + Virtualizor)

```bash
curl -fsSL https://raw.githubusercontent.com/TheTixoCloud/tixo-renewal/v1.1.0/deploy/bootstrap.sh | sudo bash
```

This is pinned to the `v1.1.0` release tag (never a mutable branch), downloads the release
tarball and its published `SHA256SUMS` from that *same* immutable release, verifies the
checksum locally, then runs `install.sh`. After install, manage everything through:

```bash
tixo-renewal           # interactive menu: dashboard, credentials, gift/interval config,
                        # enable/disable auto-renewal, logs, updates, backup/restore, uninstall
tixo-renewal --status  # noninteractive status, for scripts/cron/monitoring
```

## Why this exists

Virtualizor does not expose a documented, provable "the monthly bandwidth reset just fired"
signal on every installation (see `docs/native-reset-investigation.md` for what was actually
checked). This project never guesses that a usage drop or a date window alone proves a
renewal happened — it only ever *proposes* a renewal computed from a verified VPS creation
date (or an explicitly configured billing anchor) and a calendar-month check, and it never
writes anything to Virtualizor without a human-approved, auditable path.

## Core rule

```
On a genuinely verified reset (real pre/post USED drop, correct calendar-anniversary window
-- never inferred from the anniversary date or an exhausted quota alone):
    remaining_gb       = max(0, old_quota_gb - pre_reset_used_gb)
    calculated_gb       = remaining_gb + configured_gift_gb      # gift may be 0
    effective_quota_gb = max(1, ceil(calculated_gb))              # integer GB, never 0
    new_usage           = unchanged (this project never writes USED)
```

Virtualizor's `bandwidth` column is integer-only and `<=0` means **unlimited**, so a
calculated result is always rounded up to a whole GB and floored at 1 — never 0, never a
fraction. Unlimited-bandwidth VPSs (now or created in the future) are permanently excluded
from automatic renewal regardless of policy. Nothing above is hardcoded otherwise — every
number is configurable at three levels, see "Policy configuration" below.

## Status

| Area | State |
|---|---|
| Accounting engine (formula, exactly-once renewal, ID-reuse safety, receipt reconciliation) | Implemented, unit-tested |
| Policy engine (global/host/VPS precedence, zero-vs-unset correctness, configurable gift incl. 0) | Implemented, unit-tested |
| Fractional-GB rounding + minimum-1GB effective quota floor | Implemented, unit-tested, **live-verified in production** |
| Unlimited-VPS permanent exclusion | Implemented, unit-tested, live-verified |
| Host-local read-only monitor + readiness notifier (SQLite ledger, no SSH) | Implemented, unit-tested, running in production on a 5-minute / 30-minute cadence with `flock`-based non-overlap protection |
| Production writer (Virtualizor admin API, crash/timeout-safe, fail-closed on uncertain outcomes) | Implemented, unit-tested with mocked HTTP, **and live-verified against a real production renewal** |
| Unattended (systemd) apply path | Implemented; ships **disabled by default** — requires an operator-created approval marker and secure credential storage before it can run at all |
| Interactive management menu (`tixo-renewal`) | Implemented, unit-tested |
| Verified against a real Virtualizor renewal event | **Yes** — a real production VPS completed a full automatic-eligible renewal cycle (genuine reset detected, formula applied, write verified, ledger recorded) |

## Quick start (read-only, safe)

```bash
# On the DEV/BUILD machine:
cd deploy && ./build_package.sh

# Copy the resulting tixo-renewal-package.tar.gz to your Virtualizor host, then
# ON THE VIRTUALIZOR HOST:
tar -xzf tixo-renewal-package.tar.gz -C /root/renewal-package
cd /root/renewal-package
bash install.sh

export TIXO_RENEWAL_INSTALL_DIR=/opt/tixo-renewal
export PYTHONPATH=/opt/tixo-renewal/lib
python3 /opt/tixo-renewal/bin/renewal_cli.py dry-run
python3 /opt/tixo-renewal/bin/renewal_cli.py policy-show
python3 /opt/tixo-renewal/bin/renewal_cli.py status
```

None of the commands above ever write to Virtualizor. See `docs/DEPLOYMENT.md` for the full
installation guide and `docs/PRODUCTION_ACTIVATION.md` for the separate, explicit steps
required before any write capability is possible.

## Policy configuration

Three levels, most specific wins: **global → per-host → per-VPS**. See
`docs/examples/policy.example.json`. Every field may be set to `0`, `false`, or `null` and
means exactly that — there is no silent fallback to a hardcoded number anywhere in the
resolution logic (see `implementation/policy.py` and its test suite for the explicit
zero-vs-unset guarantees).

## CLI commands

`install`, `status`, `dry-run`, `verify`, `backup`, `rollback`, `uninstall`, `policy-show`,
`policy-validate`, `preview`, `store-credentials`, `validate-credentials`, `diagnose-api`,
`inspect-vps`, `apply`, `apply-due`, `resolve-operation`. Every command's exact read/write
behavior is documented in `implementation/renewal_cli.py`'s module docstring and in
`docs/DEPLOYMENT.md`. The `tixo-renewal` menu wraps the common ones in a guided interface.

## Safety model

- Every command except `apply`/`apply-due` is read-only with respect to Virtualizor.
- `apply`/`apply-due` require an operator-created `renewal-config/WRITE_APPROVED` marker,
  live credentials (interactive or from a 0600-permission local store), and still re-verify
  live state immediately before and after writing.
- A crash, timeout, or partial API failure during a write is recorded as
  `UNKNOWN_NEEDS_VERIFICATION` and blocks any further attempt for that exact renewal period
  until an operator resolves it with their own live verification — it is never silently
  retried.
- A calculated quota of 0 (only reachable with a 0 GB gift and zero remaining balance) is
  floored to 1 GB **only** inside the validated renewal calculation (`apply-due`/`preview`);
  a raw manual `apply --new-quota-gb 0` typed directly still gets structurally blocked, never
  silently floored, since that path has no reliable way to prove the value was validated.
- Unlimited-bandwidth VPSs are checked and excluded **before** any policy/eligibility logic
  runs, so no policy misconfiguration (including a global `auto_renewal_enabled: true`) can
  ever route one into a write.
- Monitor, apply, and the readiness notifier are three independent systemd timers, with
  monitor+apply sharing a `flock` lock so they can never run concurrently against the same
  ledger; neither sets `Persistent=true`, so enabling them never triggers an immediate
  catch-up run.

## Testing

```bash
python3 -m pytest tests/ -v
```

See `docs/TESTING.md` for the full scenario matrix (VPS deletion/recreation, ID reuse,
leap years, month-end dates, concurrent execution, clock drift, API failures, partial
writes, top-up reconciliation, and more).

## License

MIT — see `LICENSE`.
