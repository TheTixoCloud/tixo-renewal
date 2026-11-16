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

if [ -f /etc/systemd/system/virt-renewal-monitor.timer ] || [ -f /etc/systemd/system/virt-renewal-apply.timer ] \
   || [ -f /etc/systemd/system/virt-renewal-readiness-notifier.timer ]; then
  echo "System-wide timer(s) found. As a separate explicit step, disable them yourself"
  echo "(the apply timer is write-capable -- disable it FIRST if it was ever enabled):"
  echo "  sudo systemctl disable --now virt-renewal-apply.timer virt-renewal-monitor.timer virt-renewal-readiness-notifier.timer"
  echo "  sudo rm /etc/systemd/system/virt-renewal-{apply,monitor,readiness-notifier}.{service,timer}"
  echo "  sudo systemctl daemon-reload"
  echo "(This script does not do this automatically -- stopping a running service is itself"
  echo " an action that deserves an explicit, deliberate command, not a side effect of uninstall.)"
fi

if [ -f /usr/local/bin/tixo-renewal ]; then
  if [ -w /usr/local/bin/tixo-renewal ] || [ "$(id -u)" -eq 0 ]; then
    rm -f /usr/local/bin/tixo-renewal
    echo "Removed global command: /usr/local/bin/tixo-renewal"
  else
    echo "Could not remove /usr/local/bin/tixo-renewal (not root) -- remove it yourself:"
    echo "  sudo rm /usr/local/bin/tixo-renewal"
  fi
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
