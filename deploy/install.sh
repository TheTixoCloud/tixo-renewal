#!/bin/bash
# Virtualizor Bandwidth Rollover -- host-local installer.
#
# Run this SCRIPT ITSELF directly on a Virtualizor host (e.g. 203.0.113.10 (use your own host)), from the
# directory it was copied into alongside a `lib/` subdirectory containing the Python modules.
# It installs into /opt/tixo-renewal by default. Zero third-party Python dependencies are
# used anywhere in this package (stdlib only: sqlite3, json, decimal, subprocess, urllib) --
# no pip, no venv, no internet access required on the Virtualizor host.
#
# This script ONLY:
#   - copies files into /opt/tixo-renewal
#   - creates /opt/tixo-renewal/{shadow-monitor-state,renewal-config,backups,systemd}
#   - writes a default (safe) policy.json and nodes.json if they don't already exist
#     (set TIXO_RENEWAL_MONTHLY_BASE_GB to configure the monthly base/initial grant at
#     install time instead of hand-editing policy.json afterward -- e.g.
#     TIXO_RENEWAL_MONTHLY_BASE_GB=250 ./install.sh)
#   - writes systemd unit files locally under systemd/, then (v1.10.3+, root only, opt out
#     with TIXO_RENEWAL_NO_AUTO_SYSTEMD=1) installs+enables the 4 READ-ONLY core timers
#     (monitor, inventory-sync, hostnet-sync, readiness-notifier) into /etc/systemd/system
#     for real -- never the write-capable 'apply' timer, which stays a separate, explicit,
#     confirmed step (menu: "Enable automatic renewal", or `renewal_cli.py
#     enable-auto-renewal`) that also grants WRITE_APPROVED in the same step
#
# It NEVER modifies Virtualizor itself, its cron, its database, or any VPS. None of the 4
# core timers this script activates can write anything -- each one's own ExecStart is
# read-only by construction (see renewal_cli.py's unit templates).
set -euo pipefail

INSTALL_DIR="${TIXO_RENEWAL_INSTALL_DIR:-/opt/tixo-renewal}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Tixo Renewal installer =="
echo "Install directory: $INSTALL_DIR"

# ---- environment checks (fail fast, before touching anything) -----------------------------
if [ ! -d "$SCRIPT_DIR/lib" ]; then
  echo "ERROR: $SCRIPT_DIR/lib not found. Run this script from the extracted package directory." >&2
  exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3 not found on this host." >&2
  exit 1
fi
PY_VER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "python3: $PYTHON_BIN (version $PY_VER)"

if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "OS: ${PRETTY_NAME:-unknown}"
  if [ "${ID:-}" != "ubuntu" ]; then
    echo "WARNING: this installer is tested on Ubuntu 24.04; detected ID='${ID:-unknown}'. Continuing anyway."
  fi
fi

if [ "$(id -u)" -ne 0 ] && [ -z "${TIXO_RENEWAL_INSTALL_DIR:-}" ]; then
  echo "WARNING: not running as root and no custom TIXO_RENEWAL_INSTALL_DIR set -- the"
  echo "default /opt/tixo-renewal location and the global 'tixo-renewal' command both"
  echo "require root. Re-run with sudo, or set TIXO_RENEWAL_INSTALL_DIR to a writable path."
fi

# Virtualizor Master/Slave detection: HONEST LIMITATION, stated explicitly rather than
# invented -- this checks only for the presence of a local universal.php (the Admin API
# config this project's read-only diagnostics and writer.py both depend on), which is the
# Master's own file. A genuine "is this specifically a Slave-only node" check would require
# Virtualizor-internal knowledge this project has not verified against a real Slave install,
# so it is NOT claimed here. If universal.php is absent, installation continues (local setup
# only) but is NOT a supported/tested configuration for actual renewal use.
if [ -f /usr/local/virtualizor/universal.php ] || [ -f /usr/local/emps/etc/virtualizor/universal.php ]; then
  echo "Virtualizor Admin API config (universal.php) found -- Master-compatible install detected."
else
  echo "NOTE: no Virtualizor universal.php found at the usual locations on this host."
  echo "      This is either not a Virtualizor Master, or Virtualizor is not installed here."
  echo "      Installation will continue (local setup only), but 'dry-run'/'status'/'apply'"
  echo "      will all fail to reach live VPS data until this is run ON an actual Virtualizor"
  echo "      Master host. Slave-only Virtualizor nodes are NOT a verified/supported"
  echo "      configuration for this project -- the Admin API this writer uses is a Master"
  echo "      concept, not independently confirmed against a real Slave-only installation."
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "'flock' (util-linux) not found -- required for safe non-overlapping timers."
  if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
    echo "Attempting to install it automatically (util-linux, a safe, standard Ubuntu package)..."
    apt-get update -qq && apt-get install -y -qq util-linux
    if ! command -v flock >/dev/null 2>&1; then
      echo "ERROR: automatic install of util-linux did not provide 'flock'. Install it yourself." >&2
      exit 1
    fi
    echo "'flock' is now available."
  else
    echo "ERROR: cannot auto-install (not root, or apt-get unavailable). Install util-linux yourself." >&2
    exit 1
  fi
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: systemd (systemctl) not found -- this installer requires systemd for the" >&2
  echo "monitor/apply/notifier timers. This is not something safe to auto-install." >&2
  exit 1
fi

if [ "$(uname -m)" != "x86_64" ] && [ "$(uname -m)" != "aarch64" ]; then
  echo "WARNING: untested CPU architecture '$(uname -m)' -- this is pure-Python stdlib code"
  echo "         so it should still work, but has only been verified on x86_64/aarch64."
fi

mkdir -p "$INSTALL_DIR"/{lib,bin,shadow-monitor-state,renewal-config,backups,systemd}
cp -f "$SCRIPT_DIR"/lib/*.py "$INSTALL_DIR/lib/"
cp -f "$SCRIPT_DIR"/bin/*.py "$INSTALL_DIR/bin/"
chmod +x "$INSTALL_DIR"/bin/*.py
# Static Web assets (branding, live-bandwidth.js, v1.10.0+) -- a non-.py directory the
# *.py globs above never touch; copied separately, same cp -f idempotent-overwrite
# semantics, skipped entirely on an older package that has no lib/static/ to copy from.
if [ -d "$SCRIPT_DIR/lib/static" ]; then
  mkdir -p "$INSTALL_DIR/lib/static"
  cp -f "$SCRIPT_DIR"/lib/static/* "$INSTALL_DIR/lib/static/"
fi

# renewal_cli.py lives in bin/, not lib/ -- both must be on PYTHONPATH for this import to
# resolve (bin/ also carries its own non-renewal_cli imports via the script's own directory
# when run directly, but a bare `python3 -c 'import renewal_cli'` has no "script directory"
# of its own, so bin/ must be added explicitly here).
APP_VERSION="$(PYTHONPATH="$INSTALL_DIR/lib:$INSTALL_DIR/bin" "$PYTHON_BIN" -c 'import renewal_cli; print(renewal_cli.VERSION)')"
echo "TixoCloud Control Center: v$APP_VERSION"

# Idempotent: the CLI's own `install` subcommand writes default configs only if absent, and
# re-creates the SQLite schema only if missing (CREATE TABLE IF NOT EXISTS everywhere).
export TIXO_RENEWAL_INSTALL_DIR="$INSTALL_DIR"
PYTHONPATH="$INSTALL_DIR/lib" "$PYTHON_BIN" "$INSTALL_DIR/bin/renewal_cli.py" install

# v1.10.3: automatically install+enable the four READ-ONLY core timers (monitor,
# inventory-sync, hostnet-sync, readiness-notifier) into real systemd -- this is exactly the
# error-prone manual `cp`/`systemctl` sequence a real installation previously required by
# hand, one unit at a time, with some missed entirely. Requires root (writes to
# /etc/systemd/system); gracefully skipped (never fatal) when not root, or when explicitly
# opted out via TIXO_RENEWAL_NO_AUTO_SYSTEMD=1 for an operator who wants full manual control.
# The write-capable apply timer is deliberately NEVER touched here -- see the menu's "Enable
# automatic renewal" screen / `renewal_cli.py enable-auto-renewal`, which is the only
# supported way to activate it, together with granting WRITE_APPROVED.
ACTIVATED_CORE_TIMERS=0
if [ -n "${TIXO_RENEWAL_NO_AUTO_SYSTEMD:-}" ]; then
  echo
  echo "TIXO_RENEWAL_NO_AUTO_SYSTEMD set -- skipping automatic core-timer activation."
elif [ "$(id -u)" -eq 0 ]; then
  echo
  echo "Activating the 4 read-only core timers (monitor, inventory-sync, hostnet-sync,"
  echo "readiness-notifier) into systemd..."
  if PYTHONPATH="$INSTALL_DIR/lib" "$PYTHON_BIN" "$INSTALL_DIR/bin/renewal_cli.py" install-activate-core-timers; then
    ACTIVATED_CORE_TIMERS=1
  else
    echo "WARNING: automatic core-timer activation reported problems (see above). The"
    echo "install itself still succeeded -- run 'renewal_cli.py install-activate-core-timers'"
    echo "yourself to retry, or 'renewal_cli.py install-health-check' to see current status."
  fi
else
  echo
  echo "Not running as root -- skipped automatic core-timer activation (requires writing to"
  echo "/etc/systemd/system). Re-run as root, or run yourself later:"
  echo "  sudo renewal_cli.py install-activate-core-timers"
fi

# Global `tixo-renewal` command -> ONE unified entry point (v1.10.6+):
#   - no arguments:  launches the interactive management menu (unchanged behavior)
#   - any arguments: dispatched to renewal_cli.py's real scriptable subcommands, with
#     PYTHONPATH/TIXO_RENEWAL_INSTALL_DIR already correctly set -- this is what makes
#     `tixo-renewal status`/`tixo-renewal install-health-check`/`tixo-renewal backup`/etc.
#     work as genuine global commands, instead of requiring the operator to manually
#     export PYTHONPATH and invoke renewal_cli.py by its full path (a real, reported gap:
#     every piece of this project's own documentation told operators to run
#     `renewal_cli.py <command>` as if it were already a global command, when no such
#     global binary existed at all before this fix).
# Only attempted if /usr/local/bin is writable (root, or a writable custom prefix); never
# fatal if not -- the operator can always run menu.py/renewal_cli.py directly.
WRAPPER_PATH="/usr/local/bin/tixo-renewal"
if [ -w "$(dirname "$WRAPPER_PATH")" ] || [ "$(id -u)" -eq 0 ]; then
  cat > "$WRAPPER_PATH" <<WRAPPER_EOF
#!/bin/bash
export TIXO_RENEWAL_INSTALL_DIR="$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR/lib:$INSTALL_DIR/bin"
# menu.py itself already handles --status/--version/--cli as special noninteractive flags
# (documented, pre-existing, tested) -- those three, and the no-argument case, still go to
# menu.py. Any OTHER argument is a real renewal_cli.py subcommand name (status, backup,
# install-health-check, readiness, doctor, logs, enable-auto-renewal, ...).
case "\${1:-}" in
  ""|"--status"|"--version"|"--cli")
    exec "$PYTHON_BIN" "$INSTALL_DIR/lib/menu.py" "\$@"
    ;;
  *)
    exec "$PYTHON_BIN" "$INSTALL_DIR/bin/renewal_cli.py" "\$@"
    ;;
esac
WRAPPER_EOF
  chmod +x "$WRAPPER_PATH"
  echo "Installed global command: $WRAPPER_PATH"
else
  echo "Could not write to /usr/local/bin (not root) -- skipped installing the global"
  echo "'tixo-renewal' command. Run the menu directly instead:"
  echo "  TIXO_RENEWAL_INSTALL_DIR=$INSTALL_DIR PYTHONPATH=$INSTALL_DIR/lib $PYTHON_BIN $INSTALL_DIR/lib/menu.py"
fi

echo
echo "== Install complete =="
echo "Management menu (dashboard, config, enable/disable, backup/restore, uninstall):"
echo "  tixo-renewal"
echo "Noninteractive status (for scripts/automation):"
echo "  tixo-renewal --status"
echo
echo "Review and edit before anything else matters:"
echo "  $INSTALL_DIR/renewal-config/policy.json   (bandwidth policy -- starts SAFE: auto_renewal_enabled=false)"
echo "  $INSTALL_DIR/renewal-config/nodes.json    (this host, mode=local)"
echo
echo "Dry-run (read-only, safe to run any time):"
echo "  TIXO_RENEWAL_INSTALL_DIR=$INSTALL_DIR PYTHONPATH=$INSTALL_DIR/lib $PYTHON_BIN $INSTALL_DIR/bin/renewal_cli.py dry-run"
echo
if [ "$ACTIVATED_CORE_TIMERS" -eq 1 ]; then
  echo "The 4 read-only core timers (monitor, inventory-sync, hostnet-sync, readiness-notifier)"
  echo "are now installed, enabled, and running -- verify any time with:"
  echo "  tixo-renewal --status   # or: renewal_cli.py install-health-check"
else
  echo "To activate the 4 READ-ONLY core timers (never writes anything), as root:"
  echo "  sudo renewal_cli.py install-activate-core-timers"
fi
echo
echo "Production WRITE capability (the 5th timer, 'apply') remains permanently blocked until"
echo "you deliberately grant it -- from the menu: 'Enable automatic renewal', or:"
echo "  renewal_cli.py enable-auto-renewal --yes"
echo "...and even then, every write still re-verifies live state immediately before/after."
echo
echo "Optional modules (ALL disabled by default; each needs its own explicit enable step"
echo "from the 'tixo-renewal' menu before its generated systemd unit does anything):"
echo "  Telegram bot          -> menu: 'Configure Telegram bot (optional)'"
echo "  Web Control Center    -> menu: 'Configure Web Control Center (optional)'"
echo "  Relay worker          -> runs on a SEPARATE host; see 'Configure Relay Worker' there"
echo "  Remote agent          -> menu: 'Configure Remote Agent (optional, read-only)'"
echo "Their generated-but-not-installed systemd units are at $INSTALL_DIR/systemd/virt-renewal-{telegram-bot,web,relay-worker,agent}.service"

# ---- open the management menu, but ONLY in a genuinely interactive session ----------------
# Reaching this line at all already means install succeeded (set -euo pipefail aborted the
# script on any earlier failure) -- the only remaining question is whether to open the menu.
#
# `curl ... | sudo bash` (the documented one-liner) feeds THIS SCRIPT's own source through
# stdin -- by the time execution reaches here, stdin is already exhausted/closed as a
# pipe, so `[ -t 0 ]` is useless for detecting "is a human watching this". `[ -t 1 ]`
# (stdout still a terminal) is the right first signal; `/dev/tty` -- the controlling
# terminal device, independent of stdin/stdout redirection -- is what the menu's own
# input()/getpass() calls actually need to work, so both are required together. A
# noninteractive run (CI, cron, `ssh host 'curl ... | bash'` without a pty, a script
# capturing output) will fail one or both checks and skip the menu, exactly as required.
echo
if [ -n "${TIXO_RENEWAL_NO_AUTO_MENU:-}" ]; then
  echo "TIXO_RENEWAL_NO_AUTO_MENU set -- skipping automatic menu launch."
elif [ -t 1 ] && [ -r /dev/tty ] && [ -w /dev/tty ] 2>/dev/null; then
  echo "Launching the TixoCloud management menu..."
  if [ -x "$WRAPPER_PATH" ]; then
    exec "$WRAPPER_PATH" < /dev/tty > /dev/tty 2> /dev/tty
  else
    export TIXO_RENEWAL_INSTALL_DIR="$INSTALL_DIR"
    exec "$PYTHON_BIN" "$INSTALL_DIR/lib/menu.py" < /dev/tty > /dev/tty 2> /dev/tty
  fi
else
  echo "Noninteractive session detected (no controlling terminal) -- skipping automatic"
  echo "menu launch. Run 'tixo-renewal' yourself (or the fallback command printed above)"
  echo "to open the management menu."
fi
