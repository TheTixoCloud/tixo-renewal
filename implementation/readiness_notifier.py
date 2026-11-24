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
DEFAULT_HEALTH_NOTIFIED_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "shadow-monitor-state", "health-notified.json")


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


def load_notified_health_problems(path: str) -> set:
    if not os.path.exists(path):
        return set()
    try:
        data = json.loads(Path(path).read_text())
        return set(data.get("problems", []))
    except (ValueError, OSError):
        return set()  # fail safe: a corrupt state file means "nothing notified yet"


def save_notified_health_problems(path: str, problems: set) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps({"problems": sorted(problems)}, indent=2) + "\n")
    os.replace(tmp, path)


def send_telegram_alert(config_dir: str, text: str) -> None:
    """Best-effort push via the SAME Telegram transport the scheduled-backup delivery and
    first-renewal-validation notifications already use (v1.10.11). Never raises -- the
    local file/syslog alert the caller already wrote is always the durable record
    regardless of whether this push succeeds; no push channel configured is the normal,
    silent no-op case, not an error."""
    try:
        import telegram_config as tgc
        import telegram_bot as tbot
        tg_settings = tgc.load_telegram_settings(config_dir)
        if not tg_settings.enabled or not tg_settings.admin_ids:
            return
        secrets_obj = tgc.load_telegram_secrets(tgc.default_secrets_path(config_dir))
        transport = tbot.build_transport(tg_settings, secrets_obj)
        for admin_id in tg_settings.admin_ids:
            transport.call("sendMessage", {"chat_id": admin_id, "text": text})
    except Exception:
        pass


def check_health_problems(config_dir: str, state_dir: str, systemd_out_dir: str,
                            attempt_self_heal: bool = True) -> tuple:
    """Read-only (install_health_check(check_systemd=True)) -- EXCEPT for one bounded,
    safe self-heal: if a CORE READ-ONLY timer (NEVER the write-capable apply timer -- see
    service_activation.CORE_READONLY_UNITS/WRITE_CAPABLE_UNIT) is found not enabled/active,
    attempts exactly ONE re-activation via the same install_and_enable_units() the
    fresh-install path already uses (this is always safe BY CONSTRUCTION: none of these
    four timers can write to Virtualizor), and reports the outcome either way. Never
    retries beyond this one attempt per call, never touches the write-capable apply timer,
    and has nothing whatsoever to do with financial writes -- no blind retry of anything
    financial happens here or anywhere this function's result flows to.

    Returns (problems, self_heal_notes) as TWO SEPARATE lists, deliberately: `problems` is
    the current, persistent unresolved-problem set used for dedup/push/recovery detection
    -- a self-heal note (e.g. "re-activated X") must NEVER appear in this diffed set, or a
    successful heal would misfire as a brand-new 'HEALTH PROBLEM' alert the very first time
    it is announced (it already stopped being a problem by the time it's reported)."""
    import installer as inst
    health = inst.health_check(config_dir, state_dir, check_systemd=True)
    problems = list(health["problems"])
    self_heal_notes = []
    if attempt_self_heal and health.get("systemd"):
        import service_activation as sa
        write_capable_timer = sa.WRITE_CAPABLE_UNIT[1]
        unhealthy_core_timers = [
            (status["service"], timer) for timer, status in health["systemd"].items()
            if timer != write_capable_timer
            and (status["is_enabled"] != "enabled"
                 or status["is_active"] not in ("active", "waiting"))
        ]
        if unhealthy_core_timers:
            result = sa.install_and_enable_units(systemd_out_dir, unhealthy_core_timers)
            healed_timer_names = {t for _s, t in unhealthy_core_timers}
            if result["ok"]:
                problems = [p for p in problems
                            if not any(t in p for t in healed_timer_names)]
                self_heal_notes.append(f"self-healed: re-activated {result['enabled']}")
            else:
                self_heal_notes.append(
                    f"self-heal attempt FAILED for {sorted(healed_timer_names)}: "
                    f"{result['problems']}")
    return problems, self_heal_notes


def run(db_path: str, node_id: str, policy_config_path: str,
        notified_state_path: str = DEFAULT_NOTIFIED_STATE_PATH,
        alert_log_path: str = DEFAULT_ALERT_LOG_PATH,
        config_dir: str = None, state_dir: str = None, systemd_out_dir: str = None,
        health_notified_state_path: str = DEFAULT_HEALTH_NOTIFIED_STATE_PATH,
        attempt_self_heal: bool = True) -> int:
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

    logger = logging.getLogger("tixo_readiness_notifier")
    logger.setLevel(logging.WARNING)
    try:
        handler = logging.handlers.SysLogHandler(address="/dev/log")
        logger.addHandler(handler)
    except OSError:
        pass  # syslog socket unavailable (e.g. in a test sandbox) -- file log below still works

    if new_alerts:
        with open(alert_log_path, "a") as f:
            for d in new_alerts:
                text = build_alert_text(
                    d["vpsid"], d.get("reason", ""), d.get("proposed_new_quota_gb", "?"),
                    d.get("renewal_period_key", ""), node_id)
                print(text)
                f.write(text + "\n")
                logger.warning(text.replace("\n", " | "))
                if config_dir:
                    send_telegram_alert(config_dir, text)
        save_notified_state(notified_state_path, notified)
    else:
        print("No new readiness alerts. (0 new PLAUSIBLE_RESET_CANDIDATE_NEEDS_HUMAN_CONFIRMATION)")

    # v1.10.11: continuous health monitoring -- independent of whether there were any
    # readiness alerts above. Requires config_dir/state_dir (the real systemd timer's unit
    # passes these; existing callers/tests that omit them keep the EXACT prior behavior,
    # readiness-candidates only).
    if config_dir and state_dir:
        resolved_systemd_out_dir = systemd_out_dir or os.path.join(
            os.path.dirname(config_dir.rstrip("/")), "systemd")
        problems_list, self_heal_notes = check_health_problems(
            config_dir, state_dir, resolved_systemd_out_dir, attempt_self_heal=attempt_self_heal)
        for note in self_heal_notes:
            print(note)
        current_problems = set(problems_list)
        previously_notified = load_notified_health_problems(health_notified_state_path)
        newly_broken = current_problems - previously_notified
        recovered = previously_notified - current_problems

        with open(alert_log_path, "a") as f:
            for p in sorted(newly_broken):
                text = f"HEALTH PROBLEM (node={node_id}): {p}"
                print(text)
                f.write(text + "\n")
                logger.warning(text)
                send_telegram_alert(config_dir, text)
            for p in sorted(recovered):
                text = f"HEALTH RECOVERED (node={node_id}): {p}"
                print(text)
                f.write(text + "\n")
                logger.warning(text)
                send_telegram_alert(config_dir, text)

        save_notified_health_problems(health_notified_state_path, current_problems)

    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the node's existing shadow-monitor SQLite file")
    ap.add_argument("--node", required=True)
    ap.add_argument("--policy-config", required=True)
    ap.add_argument("--notified-state", default=DEFAULT_NOTIFIED_STATE_PATH)
    ap.add_argument("--alert-log", default=DEFAULT_ALERT_LOG_PATH)
    ap.add_argument("--config-dir", default=None,
                     help="v1.10.11: if given, also runs the continuous health check "
                          "(with bounded self-heal) and pushes alerts via Telegram if "
                          "configured, in addition to the read-only readiness check above")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--systemd-out-dir", default=None)
    ap.add_argument("--health-notified-state", default=DEFAULT_HEALTH_NOTIFIED_STATE_PATH)
    ap.add_argument("--no-self-heal", action="store_true",
                     help="report health problems without attempting the bounded "
                          "core-timer self-heal step")
    args = ap.parse_args()
    return run(args.db, args.node, args.policy_config, args.notified_state, args.alert_log,
               config_dir=args.config_dir, state_dir=args.state_dir,
               systemd_out_dir=args.systemd_out_dir,
               health_notified_state_path=args.health_notified_state,
               attempt_self_heal=not args.no_self_heal)


if __name__ == "__main__":
    sys.exit(main())
