#!/bin/bash
# Assembles the self-contained deployable bundle from implementation/ into deploy/{lib,bin}/
# and produces deploy/tixo-renewal-package.tar.gz. Run this ON THE DEV SERVER before copying
# the result to a Virtualizor host. This script itself never touches production.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMPL="$HERE/../implementation"

mkdir -p "$HERE/lib" "$HERE/bin"

# Only the runtime modules actually needed on a Virtualizor host -- no tests, no evidence,
# no reports, no dev-only scripts (loader.py/replay.py are for the dev-server's historical
# evidence replay and are deliberately NOT bundled here).
for f in engine.py models.py storage.py policy.py policy_integration.py local_source.py \
         writer.py alerts.py backup.py nodes.py shadow_monitor.py credentials.py \
         readiness_notifier.py menu.py hostinfo.py virtualizor_info.py inventory.py \
         inventory_sync.py analytics.py tui_textual.py event_history.py renewal_history.py \
         hostnet.py hostnet_sync.py jalali.py net_topology.py traffic_analytics.py \
         emergency_policy.py security.py control_center_api.py telegram_transport.py \
         telegram_config.py telegram_bot.py reports.py host_time.py web_auth.py audit_log.py \
         web_config.py web_server.py relay_worker.py agent_config.py agent_server.py; do
  cp -f "$IMPL/$f" "$HERE/lib/$f"
done
cp -f "$IMPL/renewal_cli.py" "$HERE/bin/renewal_cli.py"
cp -f "$HERE/integration_test_real_vps.py" "$HERE/bin/integration_test_real_vps.py"

chmod +x "$HERE/install.sh" "$HERE/uninstall.sh" "$HERE/bin/renewal_cli.py" "$HERE/bin/integration_test_real_vps.py"

find "$HERE/lib" "$HERE/bin" -type d -name __pycache__ -exec rm -rf {} +

tar -czf "$HERE/tixo-renewal-package.tar.gz" -C "$HERE" lib bin install.sh uninstall.sh

# SHA256SUMS is published as its own release asset alongside the tarball -- bootstrap.sh
# downloads both from the SAME immutable release tag and verifies before extracting anything.
(cd "$HERE" && sha256sum tixo-renewal-package.tar.gz > SHA256SUMS)

echo "Built: $HERE/tixo-renewal-package.tar.gz"
echo "Built: $HERE/SHA256SUMS"
echo "Contents:"
tar -tzf "$HERE/tixo-renewal-package.tar.gz"
