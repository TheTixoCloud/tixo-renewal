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
#   - writes systemd unit files locally (does NOT install them into /etc/systemd/system --
#     that remains a separate, explicit step, printed at the end)
#
# It NEVER modifies Virtualizor itself, its cron, its database, or any VPS. The installed
# software's `dry-run` is the only thing the generated systemd timer ever runs, and that is
# read-only by construction.
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

if [ ! -f /usr/local/virtualizor/universal.php ] && [ ! -f /usr/local/emps/etc/virtualizor/universal.php ]; then
  echo "NOTE: no Virtualizor universal.php found at the usual locations on this host."
  echo "      Installation will continue (this is just local setup), but 'dry-run'/'status'"
  echo "      will fail to read live VPS data until this is run ON an actual Virtualizor host."
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "ERROR: 'flock' (util-linux) not found -- required for safe non-overlapping timers." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"/{lib,bin,shadow-monitor-state,renewal-config,backups,systemd}
cp -f "$SCRIPT_DIR"/lib/*.py "$INSTALL_DIR/lib/"
cp -f "$SCRIPT_DIR"/bin/*.py "$INSTALL_DIR/bin/"
chmod +x "$INSTALL_DIR"/bin/*.py

# Idempotent: the CLI's own `install` subcommand writes default configs only if absent, and
# re-creates the SQLite schema only if missing (CREATE TABLE IF NOT EXISTS everywhere).
export TIXO_RENEWAL_INSTALL_DIR="$INSTALL_DIR"
PYTHONPATH="$INSTALL_DIR/lib" "$PYTHON_BIN" "$INSTALL_DIR/bin/renewal_cli.py" install

# Global `tixo-renewal` command -> launches the interactive management menu. Only attempted
# if /usr/local/bin is writable (root, or a writable custom prefix); never fatal if not --
# the operator can always run menu.py directly.
WRAPPER_PATH="/usr/local/bin/tixo-renewal"
if [ -w "$(dirname "$WRAPPER_PATH")" ] || [ "$(id -u)" -eq 0 ]; then
  cat > "$WRAPPER_PATH" <<WRAPPER_EOF
#!/bin/bash
export TIXO_RENEWAL_INSTALL_DIR="$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR/lib:$INSTALL_DIR/bin"
exec "$PYTHON_BIN" "$INSTALL_DIR/lib/menu.py" "\$@"
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
echo "To activate the periodic READ-ONLY dry-run timer (never writes anything), as a separate"
echo "explicit step once you've reviewed $INSTALL_DIR/systemd/*:"
echo "  sudo cp $INSTALL_DIR/systemd/*.service $INSTALL_DIR/systemd/*.timer /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now virt-renewal-monitor.timer"
echo
echo "Production WRITE capability remains permanently blocked until you deliberately create:"
echo "  $INSTALL_DIR/renewal-config/WRITE_APPROVED"
echo "...and even then, 'apply' requires --execute and live interactive credentials every time."
