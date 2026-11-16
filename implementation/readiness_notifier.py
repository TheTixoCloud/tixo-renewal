#!/usr/bin/env python3
"""Read-only readiness notifier for Tixo Renewal.

Does NOT poll Virtualizor, does NOT touch the network, does NOT open an SSH connection, and
does NOT ingest a new snapshot. It only re-reads the LOCAL SQLite ledger that the existing
virt-renewal-monitor timer already populates (same file, same tables, zero new load on
Virtualizor) and checks whether any VPS has newly reached
PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION.

Deduplication: a small local JSON file (NOT the renewal operations ledger, NOT policy.json,
NOT nodes.json) records which (vpsid, renewal_period_key) pairs have already been notified,
so the same real event is never reported twice, even across many runs.

Notification channel: this script writes a human-readable alert to a local file AND to the
system journal (syslog). No email/Slack/webhook integration is configured here -- none was
authorized, and this script deliberately does not invent one. If a real push channel
(email with working relay, Slack/webhook URL, etc.) is later authorized, send the same
alert_text this script already produces through it; nothing else needs to change.

Never writes to Virtualizor, never touches policy.json/nodes.json/WRITE_APPROVED/operations,
never requires credentials.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import shadow_monitor as sm
import policy as pol

DEFAULT_NOTIFIED_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "shadow-monitor-state", "readiness-notified.json")
DEFAULT_ALERT_LOG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "shadow-monitor-state", "readiness-alerts.log")


def load_notified_state(path: str) -> set:
    if not os.path.exists(path):
        return set()
    try:
        data = json.loads(Path(path).read_text())
        return set(tuple(x) for x in data.get("notified", []))
    except (ValueError, OSError):
        return set()  # fail safe: a corrupt state file means "nothing notified yet", never crash


def save_notified_state(path: str, notified: set) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = {"notified": [list(k) for k in sorted(notified)]}
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def build_alert_text(vpsid: int, reason: str, proposed_new_quota_gb: str,
                      renewal_period_key: str, node_id: str) -> str:
    return (
        f"READINESS ALERT: vpsid={vpsid} on node={node_id} reached "
        f"PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION\n"
        f"  renewal_period_key: {renewal_period_key}\n"
        f"  evidence: {reason}\n"
        f"  proposed_new_quota_gb (policy-resolved, NOT applied): {proposed_new_quota_gb}\n"
        f"\n"
        f"  PROPOSED SAFE SINGLE-VPS PILOT PLAN (manual, operator-executed, nothing here is "
        f"automatic):\n"
        f"    1. Re-verify read-only: `renewal_cli.py status` / `preview --node {node_id} "
        f"--vpsid {vpsid}`.\n"
        f"    2. Set auto_renewal_enabled=true for ONLY this VPS in vps_overrides "
        f"(policy.json) -- never globally.\n"
        f"    3. touch renewal-config/WRITE_APPROVED && chmod 600 renewal-config/WRITE_APPROVED\n"
        f"    4. `apply --node {node_id} --vpsid {vpsid} --period-key {renewal_period_key} "
        f"--new-quota-gb {proposed_new_quota_gb} --execute` (manual, typed APPLY confirmation).\n"
        f"    5. Verify the live quota matches, then `rm renewal-config/WRITE_APPROVED` "
        f"immediately.\n"
        f"    6. Only after this succeeds for one full cycle, consider widening scope.\n"
    )


def run(db_path: str, node_id: str, policy_config_path: str,
        notified_state_path: str = DEFAULT_NOTIFIED_STATE_PATH,
        alert_log_path: str = DEFAULT_ALERT_LOG_PATH) -> int:
    if not os.path.exists(db_path):
        print(f"No ledger yet at {db_path} -- nothing to check.")
        return 0

    policy_config = None
    if os.path.exists(policy_config_path):
        try:
            policy_config = pol.load_policy_config(policy_config_path)
        except pol.PolicyValidationError as e:
            print(f"WARNING: policy.json invalid ({e}); proceeding without policy-resolved numbers.")

    conn = sm.open_db(db_path)
    detections = sm.detect_renewal_candidates(conn, node_id=node_id, policy_config=policy_config)
    conn.close()

    notified = load_notified_state(notified_state_path)
    new_alerts = []
    for d in detections:
        if d["status"] != "PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION":
            continue
        key = (node_id, d["vpsid"], d.get("renewal_period_key") or "")
        if key in notified:
            continue
        new_alerts.append(d)
        notified.add(key)

    if not new_alerts:
        print("No new readiness alerts. (0 new PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION)")
        return 0

    logger = logging.getLogger("tixo_readiness_notifier")
    logger.setLevel(logging.WARNING)
    try:
        handler = logging.handlers.SysLogHandler(address="/dev/log")
        logger.addHandler(handler)
    except OSError:
        pass  # syslog socket unavailable (e.g. in a test sandbox) -- file log below still works

    with open(alert_log_path, "a") as f:
        for d in new_alerts:
            text = build_alert_text(
                d["vpsid"], d.get("reason", ""), d.get("proposed_new_quota_gb", "?"),
                d.get("renewal_period_key", ""), node_id)
            print(text)
            f.write(text + "\n")
            logger.warning(text.replace("\n", " | "))

    save_notified_state(notified_state_path, notified)
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the node's existing shadow-monitor SQLite file")
    ap.add_argument("--node", required=True)
    ap.add_argument("--policy-config", required=True)
    ap.add_argument("--notified-state", default=DEFAULT_NOTIFIED_STATE_PATH)
    ap.add_argument("--alert-log", default=DEFAULT_ALERT_LOG_PATH)
    args = ap.parse_args()
    return run(args.db, args.node, args.policy_config, args.notified_state, args.alert_log)


if __name__ == "__main__":
    sys.exit(main())
