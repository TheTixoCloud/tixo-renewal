# Virtualizor Bandwidth Rollover

A host-local, policy-driven bandwidth carryover/renewal system for Virtualizor KVM VPS hosts.
Runs directly on each Virtualizor master (no agent inside customer VPSs, no runtime
dependency on any external server), with per-host and per-VPS configurable policies, a
durable SQLite ledger, and production writes disabled until an operator explicitly approves
them.

## Why this exists

Virtualizor does not expose a documented, provable "the monthly bandwidth reset just fired"
signal on every installation (see `docs/native-reset-investigation.md` for what was actually
checked). This project never guesses that a usage drop or a date window alone proves a
renewal happened — it only ever *proposes* a renewal computed from a verified VPS creation
date (or an explicitly configured billing anchor) and a calendar-month check, and it never
writes anything to Virtualizor without a human-approved, auditable path.

## Core rule

```
On a verified monthly billing anniversary:
    new_quota = min(unused_remaining, carryover_cap_or_unlimited) + configured_monthly_grant
    new_usage = 0
```

Nothing here is hardcoded. Every number above is configurable at three levels — see
"Policy configuration" below.

## Status

| Area | State |
|---|---|
| Accounting engine (formula, exactly-once renewal, ID-reuse safety, receipt reconciliation) | Implemented, unit-tested |
| Policy engine (global/host/VPS precedence, zero-vs-unset correctness) | Implemented, unit-tested |
| Host-local read-only monitor (SQLite ledger, no SSH, no external dependency) | Implemented, unit-tested |
| Production writer (Virtualizor admin API, crash/timeout-safe) | Implemented, unit-tested with mocked HTTP — **never run against a real server by this project's own tests** |
| Unattended (systemd) apply path | Implemented, but **ships disabled**; requires an operator-created approval marker and secure credential storage before it can run at all |
| Verified against a real Virtualizor renewal event | **Not yet** — no real, naturally-occurring renewal has been observed during development |

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
`policy-validate`, `preview`, `store-credentials`, `apply`, `apply-due`, `resolve-operation`.
Every command's exact read/write behavior is documented in `implementation/renewal_cli.py`'s
module docstring and in `docs/DEPLOYMENT.md`.

## Safety model

- Every command except `apply`/`apply-due` is read-only with respect to Virtualizor.
- `apply`/`apply-due` require an operator-created `renewal-config/WRITE_APPROVED` marker,
  live credentials (interactive or from a 0600-permission local store), and still re-verify
  live state immediately before and after writing.
- A crash, timeout, or partial API failure during a write is recorded as
  `UNKNOWN_NEEDS_VERIFICATION` and blocks any further attempt for that exact renewal period
  until an operator resolves it with their own live verification — it is never silently
  retried.

## Testing

```bash
python3 -m pytest tests/ -v
```

See `docs/TESTING.md` for the full scenario matrix (VPS deletion/recreation, ID reuse,
leap years, month-end dates, concurrent execution, clock drift, API failures, partial
writes, top-up reconciliation, and more).

## License

MIT — see `LICENSE`.
