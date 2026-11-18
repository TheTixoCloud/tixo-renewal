# TixoCloud Control Center v1.9.0 — UI/UX Overhaul Release Notes

`renewal_cli.VERSION = "1.9.0"`. Built from the final source after the complete
QA pass described below.

**Local status at time of writing: built, tested, and packaged locally
(`deploy/tixo-renewal-package.tar.gz`, `deploy/SHA256SUMS`). Not yet tagged, not
yet published as a GitHub Release, not yet deployed to production (ir1). A
disposable, localhost-only preview was run on the dev host for visual
inspection — see "Preview" below. Publishing and production deployment each
require a SEPARATE, explicit operator approval.**

## What's in this release (since v1.8.0)

- **Shared traffic formatting layer (`traffic_format.py`)**: the ONE place both
  the CLI (`menu.py`) and the Web Control Center now convert raw byte
  counts/rates into human-readable strings. Decimal (SI) GB/TB for cumulative
  totals, bits/sec (Mbps/Gbps) for rates, a documented no-misleading-rounding
  fix at tier boundaries, and a `combine()` helper that reports "unavailable"
  rather than ever silently treating a missing RX or TX side as zero. `menu.py`
  now aliases this module instead of keeping its own private copy — zero
  behavioral change, confirmed by the full pre-existing CLI test suite passing
  unchanged.
- **Web Control Center visual redesign**: a genuinely new design system (richer
  dark/light palettes, KPI/metric cards, compact gauges, sticky dense tables,
  badge pills, button variants, responsive breakpoints) — still pure CSS, zero
  external fonts/icon packs/CDN. All 17 pages rebuilt on top of it: Overview is
  now a real KPI dashboard (fleet counts, CPU load, memory, uptime, per-period
  traffic with honest coverage badges); Live Host Monitoring, Network Traffic,
  Traffic Analytics, VPS Inventory, VPS Detail, Renewal Operations, Emergency
  Bandwidth, Manual Review, Alerts, Reports, Telegram Settings, Backups, System
  Settings, User Management, Audit Logs, and Service Health all replaced their
  former raw-dict/plain-card rendering with the new component set. Every
  Download/Upload/Combined figure anywhere on the Web side now goes through
  `traffic_format.py`.
- **Real charts (`charts.py`)**: pure-stdlib inline SVG daily bar charts (RX/TX
  grouped bars, gridlines, legend, native `<title>` tooltips) built on a new
  `traffic_analytics.daily_series()` function that buckets real hostnet samples
  into local-calendar days. A day with zero or discontinuity-affected samples
  renders visibly dimmer and is never silently omitted or interpolated. Wired
  into the Overview (7-day) and Traffic Analytics (7d/30d toggle) pages.
- **CLI option 4 (network interfaces) rebuilt**: a proper looping submenu —
  Main (default) / All / Physical only / Bridges only / Virtual only / Search /
  Back. Pressing Enter is equivalent to Main. Main uses actual interface
  classification (`default_shown_interfaces()`), never a hardcoded name, which
  is why it naturally resolves to just the real physical NIC + bridge (e.g.
  `ens1f1` + `viifbr0` in production) instead of dumping 60+ per-VM virtual
  interfaces. Never alters which interface the hostnet sampler reads.
- **CLI option 5 (VPS inventory table) column order fixed**: wide/medium tiers
  now show the exact required column set — `VPS ID | Name | IP | Status | Used
  | Limit | Usage %` — with Used and Limit as separate, properly GB-formatted
  columns (previously a merged `used/quota` string). Progress bars stay compact
  (a single `render_progress_bar()` call, no duplicated percentage text — a bug
  introduced and caught by a new test during this same work).
- **Localization (`web_server.UI` / `_t()`)**: a UI string catalog covering
  page headings, section titles, KPI labels, table column headers, and
  status/coverage badge text, applied across all 17 pages. Page headings are
  now derived automatically from the already-existing nav label catalog so
  every standard page's `<h1>` is localized with no per-page wiring; dynamic
  titles (e.g. "VPS 42") are left untouched by design. Verified for every
  (en/fa) × (dark/light) combination across all 17 routes.
- **Inventory IP bug fix**: the VPS Inventory table was reading a
  nonexistent `ip` field (the real field is `ip_primary`) and would have
  silently shown "None" for every VPS's IP address in production. Caught
  during manual rendering verification, not by a pre-existing test.

## Explicitly NOT touched in this release

- **Telegram connectivity**: untouched. The operator's SOCKS5 proxy
  connectivity from ir1 has not been re-verified and must not be assumed
  fixed; Telegram polling/activation stays gated until a real `getMe` check
  succeeds through a freshly rotated, operator-entered credential.
- **HTTPS / public Web exposure**: untouched. The Web preview stays
  localhost-only. No certificate was issued, no public listener was enabled,
  port 443 was not used.
- **Virtualizor itself, customer VPS bandwidth quotas/networking, renewal
  policy, the five existing systemd timers**: not modified by any change in
  this release.

## Testing

Full regression suite: **1270 passed, 0 failed** (`pytest tests/`), including
68 new localization checks (17 routes × 2 languages × 2 themes), 10 new CLI
option-4 submenu tests, 5 new CLI option-5 column-order tests, 17 new
`traffic_format.py` tests, 6 new `charts.py` tests, and 3 new
`traffic_analytics.daily_series()` tests. Every redesigned Web page was also
rendered and visually inspected (not just checked for HTTP 200) against
synthetic data covering running/stopped/suspended VPSs, unlimited quotas, and
missing/partial traffic coverage.

## Preview

A disposable, localhost-only preview (bound to `127.0.0.1`, never a public
interface) was run on the development host directly from the real
`renewal_cli.py web-run` codepath against synthetic data (60 VPS records
matching production's fleet size, 30 days of hostnet samples) — never against
any copy of real customer or production data, and never on ir1. The temporary
admin account's password was generated for this session only and is not
reused anywhere. See the accompanying session report for exact access
instructions.

## Deployment status

Not deployed. `deploy/tixo-renewal-package.tar.gz` + `deploy/SHA256SUMS` build
reproducibly from this source (verified). Deploying to ir1 requires a separate,
explicit operator approval, and must follow the existing pre-deployment
checklist (backup verification, rollback path, schema compatibility, existing
Web/Telegram settings preserved, all 5 timers unchanged, no customer VPS
operation triggered).
