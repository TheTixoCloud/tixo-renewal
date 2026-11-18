# TixoCloud Control Center v1.9.1 — Patch Release Notes

`renewal_cli.VERSION = "1.9.1"`. A narrowly scoped bugfix patch on top of v1.9.0.

**Local status at time of writing: built, tested, and packaged locally. Not yet
tagged, not yet published as a GitHub Release, not yet deployed to production.**

## What's in this release (since v1.9.0)

One fix, found during v1.9.0's own production validation on ir1:

- **`hostinfo._is_uninformative()` now recognizes `"down"` as a real, readable
  interface operstate, not just "state unreadable".** The previous check only
  matched `state == UNAVAILABLE` (the operstate file itself unreadable), so an
  unused physical NIC bay with `state="down"`, zero traffic counters, and no
  IPv4 address was never classified as uninformative. On ir1 this meant CLI
  option 4's "Main" default view showed 7 interfaces (`eno1`, `eno2`, `eno3`,
  `eno4`, `ens1f0`, `ens1f1`, `viifbr0`) instead of the intended 2
  (`ens1f1`, `viifbr0`) — the four unused `eno*` bays and `ens1f0` cluttered
  the default view alongside the one NIC and bridge actually carrying traffic.
  The classification logic was extracted into a standalone, directly unit-
  tested function (`hostinfo._is_uninformative(state, ipv4, rx_bytes,
  tx_bytes, is_virtual)`) so `get_network_interfaces()`'s inline check and the
  test suite can never drift apart again.
- **The fix is deliberately conservative**: an interface is only hidden if it
  is non-virtual, in a down-or-unreadable state, has no IPv4 address, AND has
  zero (or unreadable) RX/TX counters — all four conditions. A down interface
  that still has a static IP, or non-zero historical counters, is NOT hidden.
  This means the fix can only ever shrink the "Main" view by removing
  interfaces that were already contributing nothing useful — it cannot hide a
  legitimately active or informative interface, regardless of how unusual its
  name looks (classification is driven entirely by actual kernel-reported
  state/IP/counters, never by name pattern-matching).

## Scope discipline

This patch contains **only** the fix above, its regression tests, and this
release's own version/changelog metadata. No other file in `implementation/`
changed. Diff reviewed line-by-line before publishing; contains no secrets,
credentials, private keys, production data, or debug artifacts.

## Testing

Full regression suite: **1276 passed, 0 failed** (`pytest tests/`), including
6 new tests for `hostinfo._is_uninformative()` covering: a down+zero-counter
physical port (the exact ir1 case), an up interface with real traffic, a down
interface that still has an IP, a down interface with nonzero historical
counters, a virtual interface (handled separately regardless of state), and a
genuinely state-unreadable interface (the original, still-correct case).

## Deployment status

Not deployed as of this file's writing. `deploy/tixo-renewal-package.tar.gz` +
`deploy/SHA256SUMS` build reproducibly from this source. Deploying to ir1
follows the same documented, supported upgrade mechanism (`install.sh` /
`install-upgrade`) as v1.9.0, preceded by a fresh verified backup.
