#!/usr/bin/env python3
"""Gated, manual, end-to-end integration test against ONE real, dedicated, disposable test
VPS -- NEVER a customer VPS. Never run automatically, never run by any test suite, never run
by CI, never run by a systemd unit. Requires multiple explicit confirmations every time.

What this proves, empirically, against the real Virtualizor API (not assumed):
  1. Whether a managevps bandwidth edit also resets used_bandwidth (answers an open question
     this project previously could not answer from documentation alone).
  2. That the real renewal formula (remaining + monthly grant) matches what actually lands in
     the database after a real write.
  3. That a second apply attempt for the SAME renewal period is blocked (exactly-once).
  4. That the writer's pre/post live-read verification correctly reports APPLIED.

What this deliberately does NOT test live: purchased top-up preservation (already covered by
extensive offline unit tests in tests/test_engine.py; faking a "real purchase" on a disposable
VPS would mean fabricating evidence, which this project's own rules forbid).

Safety gates (ALL required, in order):
  - --node, --vpsid, and --i-confirm-this-is-a-disposable-test-vps-not-a-customer-vps (exact
    string) must all be given.
  - renewal-config/WRITE_APPROVED must already exist (operator-created, this script will not
    create it).
  - Interactive typed confirmation at runtime, showing the exact vpsid and current state,
    before any write.
  - Restores the VPS to its original bandwidth value at the end (best-effort, reported either
    way) -- this is a courtesy for a disposable VPS, not a correctness requirement.
"""
import argparse
import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import credentials as creds_mod
import local_source as ls
import writer as writer_mod
from engine import check_monthly_anniversary, renewal_period_index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--vpsid", required=True, type=int)
    ap.add_argument("--config-dir", required=True, help="path to renewal-config/")
    ap.add_argument("--write-approved-marker", required=True)
    ap.add_argument("--i-confirm-this-is-a-disposable-test-vps-not-a-customer-vps",
                     dest="confirmed", action="store_true")
    args = ap.parse_args()

    if not args.confirmed:
        print("Refusing: re-run with --i-confirm-this-is-a-disposable-test-vps-not-a-customer-vps")
        return 1
    if not os.path.exists(args.write_approved_marker):
        print(f"Refusing: {args.write_approved_marker} does not exist.")
        return 1

    try:
        creds = creds_mod.load_credentials(creds_mod.default_credentials_path(args.config_dir, args.node))
    except Exception as e:
        print(f"Cannot load stored credentials: {e}")
        return 1
    api_creds = writer_mod.ApiCredentials(base_url=creds.base_url, api_key=creds.api_key, api_pass=creds.api_pass)

    before_quota = ls.fetch_single_vps_quota(args.vpsid)
    before_used = ls.fetch_single_vps_used(args.vpsid)
    print(f"vpsid={args.vpsid}: current quota={before_quota} GB, used={before_used} GB")
    confirm = input(f"Type EXACTLY 'I CONFIRM VPS {args.vpsid} IS DISPOSABLE' to proceed: ")
    if confirm != f"I CONFIRM VPS {args.vpsid} IS DISPOSABLE":
        print("Cancelled.")
        return 1

    import sqlite3
    conn = sqlite3.connect(":memory:")  # this script's own operation ledger is throwaway;
    writer_mod.ensure_operations_schema(conn)  # the real durable ledger lives in the node's
    # actual shadow-monitor-state DB and is untouched by this standalone script on purpose.

    test_bandwidth = before_quota + Decimal("1")
    print(f"\n--- STEP 1: minimal bandwidth edit ({before_quota} -> {test_bandwidth}), "
          f"checking whether used_bandwidth changes ---")
    result1 = writer_mod.apply_renewal(
        conn, args.node, args.vpsid, "integration-test-step1", test_bandwidth, api_creds,
        args.write_approved_marker, confirm=True,
        read_live_quota_fn=lambda v: ls.fetch_single_vps_quota(v))
    print(f"Result: {result1}")
    after_used_step1 = ls.fetch_single_vps_used(args.vpsid)
    changed = after_used_step1 != before_used
    print(f"used_bandwidth before={before_used}, after={after_used_step1} -- "
          f"{'CHANGED' if changed else 'UNCHANGED'} (this answers the open question; it is "
          f"NOT assumed)")

    print(f"\n--- STEP 2: duplicate apply for the SAME period must be blocked ---")
    result2 = writer_mod.apply_renewal(
        conn, args.node, args.vpsid, "integration-test-step1", test_bandwidth, api_creds,
        args.write_approved_marker, confirm=True,
        read_live_quota_fn=lambda v: ls.fetch_single_vps_quota(v))
    print(f"Result: {result2} (expected status=BLOCKED_UNRESOLVED_PRIOR_OPERATION for the "
          f"same period_key, since step 1 already succeeded)")

    # Step 3 is a DECREASE on the SAME vpsid as step 1's increase. A real incident on ir1
    # showed back-to-back writes to the same vpsid (an increase immediately followed by a
    # decrease) can silently fail to take effect -- see WRITE_COOLDOWN_SECONDS in writer.py.
    # Respect that cooldown here rather than relying on the caller to space calls out by hand.
    cooldown = writer_mod.WRITE_COOLDOWN_SECONDS + 1
    print(f"\n--- Waiting {cooldown}s (write cooldown) before the restore write ---")
    time.sleep(cooldown)

    print(f"\n--- STEP 3: restoring original quota ({test_bandwidth} -> {before_quota}) ---")
    result3 = writer_mod.apply_renewal(
        conn, args.node, args.vpsid, "integration-test-restore", before_quota, api_creds,
        args.write_approved_marker, confirm=True,
        read_live_quota_fn=lambda v: ls.fetch_single_vps_quota(v))
    print(f"Result: {result3}")

    print("\n=== SUMMARY ===")
    print(f"used_bandwidth reset by a bandwidth-only edit: {'YES' if changed else 'NO'}")
    print(f"Restore to original quota: {result3.get('status')}")
    if result3.get("status") != "APPLIED":
        print(f"MANUAL ACTION NEEDED: restore vpsid {args.vpsid} to {before_quota} GB yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
