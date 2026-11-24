# TixoCloud Control Center v1.10.12 — P0 Financial Rollover Incident, Fixed

`renewal_cli.VERSION = "1.10.12"`. Fixes a **confirmed real production incident** on IR1
(2026-11-24): three VPS (62, 63, 64) had genuine billed bandwidth resets correctly detected
and flagged, but were never actually renewed — "0 attempted," every apply cycle, forever.

## Root cause #1 (the real defect): the live detection window is not durable

Both `shadow_monitor.detect_renewal_candidates` and `renewal_cli._renewal_baseline_for_vpsid`
only ever look at the **two most recent** snapshots to find a reset pattern. That's correct
for catching a reset the instant it happens — but once a few more 5-minute poll cycles pass
(waiting for an operator to authorize auto-renewal, a missed apply cycle, a restart, an
upgrade), that sliding window moves past the reset entirely. The event becomes permanently
invisible to the real write path, even though the full snapshot history still contains it.

Confirmed directly on IR1: the readiness notifier flagged all three VPS correctly at
`2026-11-24T20:38:17Z`. Automatic renewal wasn't authorized until roughly 40 minutes later —
by then, 8+ more poll cycles had already moved the window past the reset for good.

## Root cause #2 (not a defect — a real timeline artifact)

The notifier's proposed 965/3221/1908 GB vs. the "expected" 1065/3321/2008 GB is **not** a
calculation bug. `policy.json`'s `monthly_grant_gb` was genuinely `"0"` at `20:38:17Z`
(confirmed directly via IR1's own pre-v1.10.9-upgrade backup archive), and was only corrected
to `"100"` afterward, at `23:32:47Z`. The system computed exactly what the policy in effect at
that moment said — this is precisely why the fix below recomputes fresh against whatever
policy is *current at write time*, never a cached number from detection time.

## Fix

`renewal_checkpoints` gets three new durable columns (`old_quota_gb`, `old_used_gb`,
`event_ts`) via an idempotent migration. `apply_due` now consults this durable backlog
**before** falling back to the live sliding window — a delayed authorization, a missed
cycle, a restart, or an upgrade can never silently lose a correctly-detected renewal again.
An entry is marked consumed once handed to the real write path, regardless of outcome — the
`operations` table's own independent exactly-once guard is never duplicated or bypassed.

## Recovery for rows flagged by pre-v1.10.12 code (IR1's actual current state)

New `reconcile-stale-renewals` command (read-only by default; `--backfill` to act) recovers
the original baseline from full snapshot history — never deleted, unlike the live window —
and writes it back. After that, the **next** `apply-due` cycle finishes the recovery
automatically, through the exact same safety machinery (fresh policy recompute, pre/post
live verification, exactly-once) as any other renewal. Never writes to Virtualizor itself,
never decides a quota, never bypasses anything.

**Verified end-to-end against the actual built v1.10.12 package**, using the real IR1 numbers
(quota 3058/12736/9500 GB, used 2093.93/9515.07/7592.81 → 0.18/1.29/1.32 GB): reproduced the
exact "0 attempted" bug first, then `reconcile-stale-renewals --backfill` followed by a real
`apply-due` run correctly applied all three to **1065 / 3321 / 2008 GB** — exactly the
expected values — exactly once each, config and secrets untouched. A second `apply-due` run
confirmed zero re-application.

## Also fixed: contradictory status/readiness reporting

`status`'s "Frozen-for-review entries" used to lump genuinely ambiguous cases together with
plain ordinary usage growth (the single most common steady state, which `readiness` already
correctly calls `MONITORING`) — an entirely healthy fleet looked identical to one with real
unresolved problems. Now split and cross-referenced.

## IR2 audit

Read-only inspection confirmed: same latent defect present in the code (not yet triggered —
no reset has occurred there yet, only ~7 hours of history). IR2 currently has
`auto_renewal_enabled=true` and `WRITE_APPROVED` present, so it was genuinely exposed to this
exact failure mode for any future reset, until upgraded.

## Testing

Every new behavior's regression test confirmed genuinely falsifiable (reverted and
re-verified failing, then restored and verified passing): full reproduction of the real
incident timeline through `cmd_apply_due` using the real recorded numbers, backlog durability
across a simulated process restart, fail-closed behavior when policy later excludes a backlog
entry, and the stale-row recovery tool.

Full regression suite: **1450 passed, 0 failed**. Reproducible build verified.

## Deployment status

Not deployed to `ir1` or `ir2` by this session. Production access remained strictly
read-only throughout this investigation; no database, configuration, or customer quota was
modified on either node.

**Exact upgrade command:**

```bash
tixo-renewal update-apply --yes
```

**Exact recovery procedure for the three affected VPS (run after upgrading):**

```bash
tixo-renewal reconcile-stale-renewals --node self            # dry-run: shows what it found
tixo-renewal reconcile-stale-renewals --node self --backfill  # writes the recovered baseline
tixo-renewal apply-due --node self --non-interactive          # processes it now (or wait for the timer)
tixo-renewal logs                                             # verify: APPLIED, 1065/3321/2008 GB
```
