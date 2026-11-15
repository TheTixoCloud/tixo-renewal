#!/bin/bash
# Virtualizor Bandwidth Rollover -- host-local uninstaller.
# Run directly on the Virtualizor host where install.sh was run.
set -euo pipefail

INSTALL_DIR="${TIXO_RENEWAL_INSTALL_DIR:-/opt/tixo-renewal}"
PURGE=0
for arg in "$@"; do
  [ "$arg" = "--purge" ] && PURGE=1
done

echo "== Virtualizor Bandwidth Rollover uninstaller =="
echo "Install directory: $INSTALL_DIR"

if [ -f /etc/systemd/system/virt-renewal-monitor.timer ]; then
  echo "System-wide timer found. As a separate explicit step, disable it yourself:"
  echo "  sudo systemctl disable --now virt-renewal-monitor.timer"
  echo "  sudo rm /etc/systemd/system/virt-renewal-monitor.{service,timer}"
  echo "  sudo systemctl daemon-reload"
  echo "(This script does not do this automatically -- stopping a running service is itself"
  echo " an action that deserves an explicit, deliberate command, not a side effect of uninstall.)"
fi

if [ ! -d "$INSTALL_DIR" ]; then
  echo "Nothing installed at $INSTALL_DIR."
  exit 0
fi

PYTHON_BIN="$(command -v python3)"
export TIXO_RENEWAL_INSTALL_DIR="$INSTALL_DIR"
if [ "$PURGE" = "1" ]; then
  PYTHONPATH="$INSTALL_DIR/lib" "$PYTHON_BIN" "$INSTALL_DIR/bin/renewal_cli.py" uninstall --purge
  rm -rf "$INSTALL_DIR"
  echo "Removed $INSTALL_DIR entirely (--purge)."
else
  PYTHONPATH="$INSTALL_DIR/lib" "$PYTHON_BIN" "$INSTALL_DIR/bin/renewal_cli.py" uninstall
  echo "Local systemd unit files removed. Ledger/config/backups kept under $INSTALL_DIR."
  echo "Re-run with --purge to remove all data too."
fi
