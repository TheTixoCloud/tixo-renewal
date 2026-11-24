#!/bin/bash
# Tixo Renewal -- one-command installer bootstrap.
#
# Pinned to a specific, immutable release tag (this file's own URL, when fetched, is itself
# pinned to that tag -- never "main"/"master", which can change underneath you between the
# moment you run the command and the moment it finishes). Downloads the release tarball AND
# its published SHA256SUMS from that SAME immutable release, verifies the checksum locally
# BEFORE extracting or executing anything from it, then runs install.sh.
#
# Usage (replace the tag in the URL if you want a different pinned version):
#   curl -fsSL https://raw.githubusercontent.com/TheTixoCloud/tixo-renewal/v1.10.4/deploy/bootstrap.sh | sudo bash
set -euo pipefail

REPO="TheTixoCloud/tixo-renewal"
TAG="${TIXO_RENEWAL_VERSION:-v1.10.4}"
ASSET="tixo-renewal-package.tar.gz"
BASE_URL="https://github.com/$REPO/releases/download/$TAG"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "== Tixo Renewal bootstrap installer ($TAG) =="
echo "Downloading $ASSET and SHA256SUMS from the $TAG release (pinned, not a mutable branch)..."

curl -fsSL -o "$WORKDIR/$ASSET" "$BASE_URL/$ASSET"
curl -fsSL -o "$WORKDIR/SHA256SUMS" "$BASE_URL/SHA256SUMS"

echo "Verifying integrity..."
(cd "$WORKDIR" && sha256sum -c --ignore-missing SHA256SUMS)

echo "Checksum verified. Extracting..."
tar -xzf "$WORKDIR/$ASSET" -C "$WORKDIR"

echo "Running install.sh..."
bash "$WORKDIR/install.sh"
