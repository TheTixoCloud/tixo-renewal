# TixoCloud Control Center v1.10.6 — Unified Global CLI & Operational Readiness

`renewal_cli.VERSION = "1.10.6"`. Fixes two real bugs confirmed on an actual production
installation (IR1) and adds the operational-visibility tooling needed to run this product
without repeated manual diagnostics.

## What's in this release (since v1.10.5)

### A. Fixed: every documented CLI command genuinely failed with "command not found"

The installed global `tixo-renewal` wrapper only ever launched the interactive menu,
passing any arguments straight through to `menu.py` -- which only understands
`--status`/`--version`/`--cli`. Every other documented command
(`renewal_cli.py install-health-check`, `backup`, `status`, ...) was never actually
reachable as a global command; an operator had to manually export `PYTHONPATH` and invoke
`renewal_cli.py` by its full path. Confirmed directly on the real IR1 installation.

Fixed `install.sh`'s wrapper template: no arguments, or one of the three existing menu
flags, still go to `menu.py` (unchanged); any other argument now dispatches to
`renewal_cli.py`'s real subcommands, with the environment already set correctly. This alone
makes `tixo-renewal status`, `tixo-renewal backup`, `tixo-renewal enable-auto-renewal`, etc.
work as genuine global commands for the first time.

### B. New: `readiness`, `doctor`, `logs`, and friendly aliases

- `tixo-renewal readiness` — per-VPS financial-engine readiness, distinct from service
  health: `COLLECTING_BASELINE` / `MONITORING` / `REVIEW_REQUIRED` /
  `RENEWAL_CANDIDATE_PENDING_REVIEW` / `MANUAL_ONLY_UNLIMITED`.
- `tixo-renewal doctor` — one human-readable report combining service health, legacy-
  automation detection, credential presence, backup freshness, and writer state.
- `tixo-renewal logs [--limit N]` — recent renewal operations and backups.
- Friendly aliases (rewritten before argparse, one implementation each):
  `health-check` → `install-health-check`, `update` → `update-apply`.

### C. Fixed a real over-broad "FROZEN_FOR_REVIEW" misclassification

Investigating a report that all 57 VPS on IR1 showed `FROZEN_FOR_REVIEW` immediately after
install surfaced two distinct things, both now handled correctly:

1. **Normal, benign, self-resolving**: a VPS with only one recorded observation has nothing
   to compare yet -- this clears automatically on the next poll and was never a real
   problem. `readiness` now labels this `COLLECTING_BASELINE`, not a generic, alarming-
   sounding status.
2. **A real, separate classification gap found while building the above**: a VPS whose
   usage simply *grew* (the single most common steady-state case in real production data)
   was *also* being folded into the same generic `FROZEN_FOR_REVIEW` label by the existing
   dry-run report, via `engine.py`'s own `NOT_A_RENEWAL`/`DUPLICATE_RENEWAL_PERIOD` outcomes
   (both completely benign -- "nothing happened" and "duplicate correctly rejected") never
   being distinguished from `UNVERIFIED_ELIGIBILITY` (a genuinely ambiguous, reset-shaped
   pattern the engine deliberately flags). `shadow_monitor.detect_renewal_candidates()` now
   additionally exposes the real `Proposal.kind` field (`engine_kind`, purely additive --
   existing `dry-run` output is unchanged), which `readiness` uses to tell these apart
   without re-deriving any financial logic.

### D. Dashboard now shows financial-engine readiness, separate from service health

`collect_dashboard_data`/`render_dashboard` surface per-node readiness counts alongside the
existing timer/operation health -- a node can be perfectly healthy at the service level
while every VPS is still collecting its first observations, and that distinction is now
visible on the dashboard itself, not just in a separate command.

## New/updated tests

Real end-to-end wrapper-dispatch tests against the actual installed global command (not a
simulation) for `status`/`install-health-check`/`health-check`/`readiness`/`doctor`/
`logs`/`backup`, plus confirmation that `--status`/`--version` still route to `menu.py`
correctly. Direct readiness-bucketing tests for baseline/monitoring/unlimited/review-
required cases (including the real steady-state-usage-growth case that exposed the
misclassification above). `doctor`/`logs` smoke tests. CLI alias rewriting tests. Dashboard
readiness-count test.

## Production investigation

Read-only re-inspection of the real, already-installed IR1 production instance confirmed
both bugs above directly (not simulated) before fixing them. No production system was
modified.

## Testing

Full regression suite: **1416 passed, 0 failed**.

## Deployment status

Not deployed to `ir1` or `ir2` by this session -- IR1 already has v1.10.5 manually
installed by the operator; this release is ready for the operator to apply via
`tixo-renewal update` (once the wrapper fix itself is in place -- see the exact upgrade
command in the closeout report) or a fresh install on IR2.
