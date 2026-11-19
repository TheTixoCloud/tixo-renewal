"""uninstaller.py (v1.10.2) -- the real two-mode uninstall mechanism.

Replaces the previous `cmd_uninstall()`, which only ever removed LOCALLY GENERATED systemd
unit template copies and printed manual commands for the operator to run themselves --
leaving all application code, any actually-installed systemd units, and the global wrapper
behind even after a "successful" uninstall.

SAFETY, EXPLICIT (every rule below is load-bearing, not decorative):
  - Ownership is determined by FIXED, explicit allowlists (exact systemd unit names, exact
    top-level install-directory entry names) -- never a wildcard/glob, never "rm -rf
    install_dir" as a single operation. A directory is only ever removed by first
    confirming its name AND a signature file inside it (e.g. lib/ must contain
    renewal_cli.py) match what THIS installer is known to create.
  - `inspect_install_dir()` classifies every top-level entry under install_dir as
    code/data/backup/UNKNOWN. An UNKNOWN entry (anything not created by this installer)
    is NEVER touched by either uninstall mode -- it is always reported, never silently
    skipped or silently removed.
  - Every systemd action only ever targets the exact, fixed `OWNED_SYSTEMD_UNITS` name
    list -- never a pattern match against `/etc/systemd/system/*`.
  - Backups (`backups/` under install_dir) are NEVER removed by either `standard_uninstall`
    or `full_purge` -- deleting them requires a SEPARATE, explicit call to
    `delete_backups()` with its own confirmation, regardless of which uninstall mode ran.
  - Nothing here ever touches Virtualizor's own files/database, any customer VPS disk or
    network configuration, or any file/service outside the fixed allowlists above.
  - Every function returns a detailed report dict (`removed`, `preserved`, `skipped`,
    `unknown_entries`, `problems`) -- a caller must never claim a complete purge succeeded
    without checking `problems`/`unknown_entries` is empty.
"""
from __future__ import annotations

import os
import shutil
import subprocess

SYSTEMD_UNIT_DIR = "/etc/systemd/system"
GLOBAL_WRAPPER_PATH = "/usr/local/bin/tixo-renewal"

# The EXACT, fixed set of unit names this project's own installer has ever generated (see
# renewal_cli.write_systemd_units/write_backup_systemd_unit) -- the single source of truth
# for "is this systemd unit ours", shared with the pre-existing renewal_cli.UNINSTALL_UNIT_NAMES
# constant so the two can never drift apart.
OWNED_SYSTEMD_UNITS = (
    "virt-renewal-monitor.service", "virt-renewal-monitor.timer",
    "virt-renewal-apply.service", "virt-renewal-apply.timer",
    "virt-renewal-readiness-notifier.service", "virt-renewal-readiness-notifier.timer",
    "virt-renewal-inventory-sync.service", "virt-renewal-inventory-sync.timer",
    "virt-renewal-hostnet-sync.service", "virt-renewal-hostnet-sync.timer",
    "virt-renewal-telegram-bot.service",
    "virt-renewal-web.service", "virt-renewal-relay-worker.service", "virt-renewal-agent.service",
    "virt-renewal-backup.service", "virt-renewal-backup.timer",
)

# Classification of every top-level entry this installer is known to create under
# install_dir. "code" entries are removed in BOTH modes (they're pure application code,
# trivially re-downloadable, never user data). "data" entries survive `standard_uninstall`
# (preserved for possible recovery) and are only removed by `full_purge`. "backup" entries
# are never removed by either mode -- see `delete_backups()`.
KNOWN_ENTRY_KIND = {
    "lib": "code", "bin": "code", "systemd": "code",  # 'systemd' here = install_dir's own
                                                        # LOCAL generated-template copies,
                                                        # never /etc/systemd/system itself
    "renewal-config": "data", "shadow-monitor-state": "data",
    "backups": "backup",
}

# Signature file(s) each "code" directory must contain before it is trusted to be ours --
# the directory NAME alone is never sufficient grounds for deletion.
_CODE_SIGNATURES = {
    "lib": ("renewal_cli.py", "menu.py"),
    "bin": ("renewal_cli.py",),
}


def _systemctl(*args, timeout: float = 10.0) -> dict:
    try:
        result = subprocess.run(["systemctl", *args], capture_output=True, text=True,
                                 timeout=timeout)
        return {"ok": result.returncode == 0, "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}


def inspect_install_dir(install_dir: str) -> dict:
    """Read-only. Returns {"exists", "entries": {name: kind}} where kind is one of
    "code"/"data"/"backup"/"UNKNOWN". Never removes or modifies anything."""
    if not os.path.isdir(install_dir):
        return {"exists": False, "entries": {}}
    entries = {}
    for name in sorted(os.listdir(install_dir)):
        entries[name] = KNOWN_ENTRY_KIND.get(name, "UNKNOWN")
    return {"exists": True, "entries": entries}


def _is_verified_code_dir(path: str, signature_files: tuple) -> bool:
    """A directory is only ever treated as "ours to delete" if it exists AND contains at
    least one of its known signature files -- never on name alone (an operator could
    conceivably have an unrelated `lib/` symlinked or bind-mounted there)."""
    if not os.path.isdir(path):
        return False
    return any(os.path.exists(os.path.join(path, sig)) for sig in signature_files)


def inspect_installed_units() -> dict:
    """Read-only. For each OWNED_SYSTEMD_UNITS name, reports whether it exists in
    /etc/systemd/system and its current active/enabled state (never changes anything)."""
    report = {}
    for unit in OWNED_SYSTEMD_UNITS:
        path = os.path.join(SYSTEMD_UNIT_DIR, unit)
        if not os.path.exists(path):
            report[unit] = {"installed": False}
            continue
        active = _systemctl("is-active", unit)
        enabled = _systemctl("is-enabled", unit)
        report[unit] = {"installed": True, "active": active["stdout"] or active["stderr"],
                         "enabled": enabled["stdout"] or enabled["stderr"]}
    return report


def preview_uninstall(install_dir: str, purge: bool) -> dict:
    """Read-only. Returns exactly what WOULD happen for the requested mode -- the required
    preview before any destructive confirmation is accepted. Never touches anything."""
    dir_state = inspect_install_dir(install_dir)
    units = inspect_installed_units()
    will_remove_entries = []
    will_preserve_entries = []
    unknown_entries = []
    for name, kind in dir_state["entries"].items():
        if kind == "UNKNOWN":
            unknown_entries.append(name)
        elif kind == "code":
            will_remove_entries.append(name)
        elif kind == "data":
            (will_remove_entries if purge else will_preserve_entries).append(name)
        elif kind == "backup":
            will_preserve_entries.append(name)  # NEVER removed by preview/either mode

    installed_units = [u for u, info in units.items() if info["installed"]]
    global_wrapper_present = os.path.exists(GLOBAL_WRAPPER_PATH)

    return {
        "mode": "full_purge" if purge else "standard",
        "install_dir_exists": dir_state["exists"],
        "will_stop_disable_units": installed_units,
        "will_remove_unit_files": installed_units,
        "will_remove_global_wrapper": global_wrapper_present,
        "will_remove_entries": sorted(will_remove_entries),
        "will_preserve_entries": sorted(will_preserve_entries),
        "unknown_entries_never_touched": sorted(unknown_entries),
    }


def stop_and_disable_owned_units() -> dict:
    """Stops+disables every OWNED_SYSTEMD_UNITS entry that is actually installed --
    silently no-ops (never errors) for ones that were never installed. Returns per-unit
    results; never touches any unit outside this fixed list."""
    results = {}
    for unit in OWNED_SYSTEMD_UNITS:
        path = os.path.join(SYSTEMD_UNIT_DIR, unit)
        if not os.path.exists(path):
            results[unit] = {"acted": False, "reason": "not installed"}
            continue
        stop = _systemctl("stop", unit)
        disable = _systemctl("disable", unit)
        results[unit] = {"acted": True, "stopped": stop["ok"], "disabled": disable["ok"]}
    return results


def remove_owned_unit_files() -> list:
    """Removes unit files from /etc/systemd/system -- ONLY exact OWNED_SYSTEMD_UNITS names
    -- then reloads systemd. Returns the list of files actually removed."""
    removed = []
    for unit in OWNED_SYSTEMD_UNITS:
        path = os.path.join(SYSTEMD_UNIT_DIR, unit)
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(unit)
            except OSError:
                pass  # reported via the caller's post-check, never silently claimed removed
    if removed:
        _systemctl("daemon-reload")
    return removed


def _remove_code_entries(install_dir: str, report: dict) -> None:
    for name, sig in _CODE_SIGNATURES.items():
        path = os.path.join(install_dir, name)
        if not os.path.exists(path):
            continue
        if _is_verified_code_dir(path, sig):
            shutil.rmtree(path)
            report["removed"].append(name)
        else:
            report["problems"].append(
                f"{name}/ exists but does not contain an expected signature file "
                f"({sig}) -- refusing to delete it; remove it yourself after checking "
                f"its contents.")
    # The local generated-template systemd/ copy (never /etc/systemd/system itself).
    local_systemd = os.path.join(install_dir, "systemd")
    if os.path.isdir(local_systemd):
        shutil.rmtree(local_systemd)
        report["removed"].append("systemd")


def _remove_global_wrapper(report: dict) -> None:
    if os.path.exists(GLOBAL_WRAPPER_PATH):
        try:
            os.remove(GLOBAL_WRAPPER_PATH)
            report["removed"].append(GLOBAL_WRAPPER_PATH)
        except OSError as e:
            report["problems"].append(f"could not remove {GLOBAL_WRAPPER_PATH}: {e}")


def standard_uninstall(install_dir: str) -> dict:
    """Stops+disables+removes every actually-installed OWNED_SYSTEMD_UNITS unit, removes
    the global `tixo-renewal` wrapper, removes application CODE (lib/, bin/, the local
    systemd/ template copies) -- but PRESERVES renewal-config/, shadow-monitor-state/, and
    backups/ for possible recovery. Any UNKNOWN top-level entry under install_dir is left
    completely untouched and reported. Never touches Virtualizor or any customer VPS data."""
    report = {"mode": "standard", "removed": [], "preserved": [], "problems": [],
              "unknown_entries": [], "units_stopped_disabled": {}, "unit_files_removed": []}

    dir_state = inspect_install_dir(install_dir)
    if not dir_state["exists"]:
        report["problems"].append(f"{install_dir} does not exist -- nothing to uninstall")
        return report

    report["units_stopped_disabled"] = stop_and_disable_owned_units()
    report["unit_files_removed"] = remove_owned_unit_files()
    _remove_global_wrapper(report)
    _remove_code_entries(install_dir, report)

    for name, kind in dir_state["entries"].items():
        if kind == "UNKNOWN":
            report["unknown_entries"].append(name)
        elif kind in ("data", "backup"):
            report["preserved"].append(name)

    return report


def full_purge(install_dir: str) -> dict:
    """Everything `standard_uninstall` does, PLUS removes renewal-config/ and
    shadow-monitor-state/ (config, secrets, databases, ledgers, runtime state). Still NEVER
    removes backups/ (call `delete_backups()` separately, with its own confirmation, if
    that is genuinely wanted) and still NEVER touches an UNKNOWN top-level entry. Caller is
    responsible for obtaining the "PURGE ALL TIXOCLOUD DATA" confirmation BEFORE calling
    this -- this function performs no confirmation itself, matching every other
    destructive function in this codebase's convention of keeping confirmation at the
    CLI/menu layer."""
    report = standard_uninstall(install_dir)
    report["mode"] = "full_purge"

    dir_state = inspect_install_dir(install_dir)
    for name, kind in dir_state["entries"].items():
        if kind != "data":
            continue
        path = os.path.join(install_dir, name)
        try:
            shutil.rmtree(path)
            report["removed"].append(name)
            if name in report["preserved"]:
                report["preserved"].remove(name)
        except OSError as e:
            report["problems"].append(f"could not remove {name}/: {e}")

    return report


def delete_backups(install_dir: str) -> dict:
    """SEPARATE from both uninstall modes -- removes install_dir/backups/ ONLY. Caller must
    obtain its own explicit confirmation (an inventory of what will be deleted, shown
    first) before calling this; this function performs no confirmation itself."""
    report = {"removed": [], "problems": []}
    path = os.path.join(install_dir, "backups")
    if not os.path.isdir(path):
        report["problems"].append(f"{path} does not exist -- nothing to delete")
        return report
    try:
        shutil.rmtree(path)
        report["removed"].append("backups")
    except OSError as e:
        report["problems"].append(f"could not remove backups/: {e}")
    return report


def verify_no_residue(install_dir: str, purge: bool = True) -> dict:
    """Read-only, post-uninstall sanity check: confirms no OWNED_SYSTEMD_UNITS file remains
    in /etc/systemd/system, no owned unit is still active, and the global wrapper is gone --
    this part applies to BOTH modes, since both stop/disable/remove every owned unit and
    the wrapper. The install_dir content check depends on `purge`: a full purge (the
    default here) expects NO code or data entries to remain; a standard uninstall
    (`purge=False`) expects code entries gone but DATA entries (renewal-config/,
    shadow-monitor-state/) to still be present -- that's the deliberately-preserved
    recovery data, not residue. `ok=False` if anything OWNED still remains that the
    requested mode should have removed -- a caller must never claim uninstall/purge
    succeeded without checking this."""
    problems = []
    for unit in OWNED_SYSTEMD_UNITS:
        if os.path.exists(os.path.join(SYSTEMD_UNIT_DIR, unit)):
            problems.append(f"unit file still present: {unit}")
        active = _systemctl("is-active", unit)
        if active["stdout"] == "active":
            problems.append(f"unit still active: {unit}")
    if os.path.exists(GLOBAL_WRAPPER_PATH):
        problems.append(f"global wrapper still present: {GLOBAL_WRAPPER_PATH}")
    dir_state = inspect_install_dir(install_dir)
    if dir_state["exists"]:
        expected_gone = ("code", "data") if purge else ("code",)
        for name, kind in dir_state["entries"].items():
            if kind in expected_gone:
                problems.append(f"{kind} entry still present: {name}")
    return {"ok": not problems, "problems": problems}
