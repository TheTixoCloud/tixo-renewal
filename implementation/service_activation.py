"""service_activation.py (v1.10.3) -- real, automatic installation/activation of the
project's own READ-ONLY systemd timers, plus read-only verification of any of this
project's units (read-only or write-capable).

Why this exists: prior releases only ever wrote systemd unit *templates* under
install_dir/systemd/ and printed the exact `cp`/`systemctl` commands for an operator to run
themselves -- "generate only, never touch the live system" was a deliberate safety stance,
but in practice it meant every real install required several manual systemctl invocations,
and it was easy to forget one unit (exactly what happened on `ir2`: inventory-sync and
hostnet-sync were missed because copying 8 files by hand is error-prone). This module
automates that mechanical copy/enable/verify work for the units that are safe to automate
-- `CORE_READONLY_UNITS` are, by construction, never able to write to Virtualizor (see each
unit's own generating template in renewal_cli.py) -- while keeping `WRITE_CAPABLE_UNIT`
(the renewal `apply` timer) deliberately excluded from automatic activation; that one is
only ever installed/enabled via the explicit, confirmed flow in
`menu.action_toggle_auto_renewal`/`renewal_cli.cmd_enable_auto_renewal`, which couples it to
creating `WRITE_APPROVED` in the same operator-confirmed step (see that function's
docstring).

SAFETY, EXPLICIT:
  - Every function here only ever touches the EXACT, fixed unit names passed to it (shared
    source of truth: `uninstaller.OWNED_SYSTEMD_UNITS`) -- never a glob/pattern match
    against /etc/systemd/system.
  - Nothing here is ever called without the caller first confirming root (enabling/starting
    a systemd unit requires it) -- these functions do not silently re-exec via sudo.
  - `verify_units_status()` is purely read-only (`systemctl is-enabled`/`is-active`/
    `is-failed`) -- it never starts, stops, enables, or disables anything.
  - Idempotent in both directions: installing/enabling an already-installed-and-enabled
    timer, or disabling/removing an already-absent one, is always safe to re-run and never
    raises for that reason alone.
  - `_systemctl()` is the single chokepoint every other function here calls through --
    tests monkeypatch this ONE function (same convention as uninstaller.py's own
    `_systemctl`) rather than mocking subprocess directly, and never touch the real
    /etc/systemd/system from automated tests (SYSTEMD_UNIT_DIR is also monkeypatchable to a
    disposable path for that reason).
"""
from __future__ import annotations

import os
import shutil
import subprocess

SYSTEMD_UNIT_DIR = "/etc/systemd/system"

# The four timers that are READ-ONLY by construction (see renewal_cli.py's own
# SERVICE_UNIT_TEMPLATE/INVENTORY_SERVICE_UNIT_TEMPLATE/HOSTNET_SERVICE_UNIT_TEMPLATE/
# NOTIFIER_SERVICE_UNIT_TEMPLATE docstrings -- none of their ExecStart commands can reach
# writer.py's write path) -- these are the ones a fresh install activates automatically.
CORE_READONLY_UNITS = (
    ("virt-renewal-monitor.service", "virt-renewal-monitor.timer"),
    ("virt-renewal-inventory-sync.service", "virt-renewal-inventory-sync.timer"),
    ("virt-renewal-hostnet-sync.service", "virt-renewal-hostnet-sync.timer"),
    ("virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer"),
)

# The one timer that CAN write to Virtualizor (via writer.py, gated on WRITE_APPROVED) --
# deliberately kept separate from CORE_READONLY_UNITS and never touched by
# install_and_enable_units() unless a caller explicitly passes it, which only
# cmd_enable_auto_renewal/action_toggle_auto_renewal(True) ever does.
WRITE_CAPABLE_UNIT = ("virt-renewal-apply.service", "virt-renewal-apply.timer")


def _systemctl(*args, timeout: float = 15.0) -> dict:
    try:
        result = subprocess.run(["systemctl", *args], capture_output=True, text=True,
                                 timeout=timeout)
        return {"ok": result.returncode == 0, "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}


def install_and_enable_units(systemd_src_dir: str, unit_pairs) -> dict:
    """Copies each (service, timer) pair from `systemd_src_dir` (install_dir/systemd/, where
    renewal_cli.write_systemd_units already wrote them) into SYSTEMD_UNIT_DIR, runs ONE
    `daemon-reload`, then `enable --now` on each timer (never the .service -- the timer unit
    is what carries `[Install] WantedBy=timers.target`; enabling the .service directly would
    not persist across reboots the way enabling its timer does). Returns a report; never
    raises for an expected failure (missing source file, systemctl error) -- every problem is
    appended to report['problems'] and report['ok'] is set False, but every OTHER pair is
    still attempted."""
    report = {"ok": True, "copied": [], "enabled": [], "problems": []}

    for service, timer in unit_pairs:
        for name in (service, timer):
            src = os.path.join(systemd_src_dir, name)
            if not os.path.exists(src):
                report["ok"] = False
                report["problems"].append(f"missing generated unit file: {src}")
                continue
            dest = os.path.join(SYSTEMD_UNIT_DIR, name)
            shutil.copy2(src, dest)
            report["copied"].append(name)

    reload_result = _systemctl("daemon-reload")
    if not reload_result["ok"]:
        report["ok"] = False
        report["problems"].append(f"systemctl daemon-reload failed: {reload_result['stderr']!r}")
        return report

    for service, timer in unit_pairs:
        if not os.path.exists(os.path.join(SYSTEMD_UNIT_DIR, timer)):
            continue  # already reported as missing above; nothing to enable
        enable_result = _systemctl("enable", "--now", timer)
        if not enable_result["ok"]:
            report["ok"] = False
            report["problems"].append(
                f"failed to enable/start {timer}: {enable_result['stderr']!r}")
        else:
            report["enabled"].append(timer)

    return report


def disable_and_remove_units(unit_pairs) -> dict:
    """Stops+disables each timer (never errors if it was never installed/enabled in the
    first place -- idempotent), then removes both unit files from SYSTEMD_UNIT_DIR if
    present. Always runs ONE final `daemon-reload` regardless of whether anything was
    actually removed, so systemd's view of the world matches the filesystem afterward."""
    report = {"ok": True, "stopped": [], "removed": [], "problems": []}

    for service, timer in unit_pairs:
        timer_path = os.path.join(SYSTEMD_UNIT_DIR, timer)
        if os.path.exists(timer_path):
            result = _systemctl("disable", "--now", timer)
            if not result["ok"]:
                report["ok"] = False
                report["problems"].append(f"failed to disable {timer}: {result['stderr']!r}")
            else:
                report["stopped"].append(timer)
        for name in (service, timer):
            path = os.path.join(SYSTEMD_UNIT_DIR, name)
            if os.path.exists(path):
                os.remove(path)
                report["removed"].append(name)

    _systemctl("daemon-reload")
    return report


def verify_units_status(unit_pairs) -> dict:
    """Read-only health snapshot of each (service, timer) pair: whether the timer is
    enabled (persists across reboot), whether it's currently active (loaded and waiting/
    running), and whether its paired service is in a *failed* state (a oneshot service is
    normally 'inactive' between runs -- 'failed' specifically means its last run exited
    nonzero and systemd has not cleared that). Never modifies anything."""
    report = {}
    for service, timer in unit_pairs:
        enabled = _systemctl("is-enabled", timer)
        active = _systemctl("is-active", timer)
        failed = _systemctl("is-failed", service)
        report[timer] = {
            "service": service,
            "is_enabled": enabled["stdout"] or enabled["stderr"],
            "is_active": active["stdout"] or active["stderr"],
            "service_is_failed": failed["stdout"] or failed["stderr"],
        }
    return report
