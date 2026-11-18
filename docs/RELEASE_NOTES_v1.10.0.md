# TixoCloud Control Center v1.10.0 — Live Bandwidth + Branding Release Notes

`renewal_cli.VERSION = "1.10.0"`. A minor release (new feature) on top of v1.9.1.

**Local status at time of writing: built, tested, and packaged locally. Not yet
tagged, not yet published as a GitHub Release, not yet deployed to production.**

## What's in this release (since v1.9.1)

- **Live Network Bandwidth widget** on the Overview and Network Traffic pages
  (`static/live-bandwidth.js`, zero dependencies, vanilla JS). Shows, updated
  roughly every 4 seconds:
  - Live Download (RX), Upload (TX), and Combined rate, switchable between
    bit-based (Mbps/Gbps) and byte-based (MB/s/GB/s) via an explicit Auto /
    Mbps / Gbps / MB/s / GB/s unit selector. Mbps is never confused with MB/s
    — a dedicated correctness test executes the real shipped JS file under
    Node and confirms the two differ by exactly 8x for the same underlying
    reading.
  - Persistent cumulative Download/Upload/Combined (the same figures already
    shown elsewhere, via `traffic_format.py`'s decimal GB/TB formatting).
  - A real-time multi-series SVG chart (Download/Upload/Combined lines) with
    an honest wall-clock time axis, a throughput axis labeled in the current
    unit, a pause/resume control, and a 1/5/15-minute window selector that
    slices the browser's own already-collected samples — it never fabricates
    or backfills history the browser hasn't actually observed.
  - Counter resets (reboot, interface replacement) are detected and reported
    as "stale" rather than silently producing a huge fabricated spike.
  - Bounded to a 300-sample ring buffer per widget instance — memory/DOM cost
    can never grow unbounded even if a tab is left open for hours.

  **No new backend process or heavy polling was added.** The widget polls the
  existing, already-authenticated `/api/live` endpoint — one cheap
  `/sys/class/net` read per request, same endpoint the Live Host Monitoring
  page already used. Every rate is computed **client-side** from the delta
  between two consecutive raw counter reads; the server never computes,
  stores, or persists a "live rate" itself.

- **Investigated the host's "suspicious" high RX/TX rate values before
  building anything on top of them** (not just reformatted): confirmed via
  `ethtool` that ir1's monitored interface (`ens1f1`) is a genuine 10GbE link
  (`ixgbe` driver, full duplex), confirmed hostnet sample deltas are
  monotonic with no discontinuities in the inspected window, and hand-
  verified `hostnet.compute_live_throughput()`'s bytes/sec → bits/sec
  conversion against the raw sample arithmetic. The observed ~5 Gbps combined
  rate is 50% of the link's physical capacity — real, physically plausible
  traffic for a 60-VPS hosting node, not a computation bug or counter-overflow
  artifact. The live widget only ever reads the single interface already
  configured for monitoring and never sums a second interface into it, so it
  can never double-count a physical NIC together with its bridge.

- **Official TixoCloud branding** — the operator-provided logo
  (`implementation/static/tixocloud.png`, light wordmark for dark theme;
  `tixocloud-dark.png`, dark wordmark for light theme — both the real
  provided artwork, never a generated substitute; originals under `/root`
  copied, never modified) now appears in the sidebar on every authenticated
  page and centered on the login page, theme-correct, aspect-ratio preserved,
  plus a `/favicon.ico`. Served through a new fixed-allowlist static route
  (`STATIC_FILES` dict in `web_server.py`) that can only ever serve the exact
  files it lists — confirmed by test that a path-traversal attempt or unknown
  filename falls through to the normal unauthenticated redirect, never reads
  the filesystem.

## Scope discipline

Nothing in this release touches renewal policy, VPS quotas, Virtualizor
integration, or the five systemd timers. No new database, no new schema, no
new systemd unit. `deploy/build_package.sh` was extended only to bundle the
new `static/` directory into `lib/static/`.

## Testing

Full regression suite: **1306 passed, 0 failed**, including:
- 16 new Web server tests (static asset serving, path-traversal safety, logo
  presence/theme-correctness, live-widget presence gated on interface
  configuration, `/api/live` auth + response shape, no config/secret leakage
  into the widget's markup).
- 9 Node-based tests executing the actual shipped `live-bandwidth.js` file
  (not a Python reimplementation) — unit-conversion correctness including the
  Mbps-vs-MB/s distinction, auto-scaling by magnitude, null/NaN handling.
- 5 static-asset integrity tests (valid PNG magic bytes, sane dimensions,
  light/dark variants share identical dimensions).

Reproducible build verified (two independent builds, identical sha256). All
17 pages re-verified end-to-end across en/fa × dark/light (no raw dicts,
logo present, correct HTTP 200).

## Deployment status

Not deployed as of this file's writing. Deploying to ir1 follows the same
documented, supported upgrade mechanism (`install.sh` / `install-upgrade`) as
prior releases, preceded by a fresh verified backup.
