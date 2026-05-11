#!/usr/bin/env bash
# Idempotent azcopy v10.22+ installer.
#
# azcopy isn't in apt (the official channel is a tarball at aka.ms/downloadazcopy-v10-linux).
# Slice 1 cloud-init installs it for fresh provisions; deploy-on-vm.sh runs this on every
# deploy as a safety net for existing VMs provisioned before Slice 4.
#
# Usage: sudo /opt/msai/scripts/install-azcopy.sh

set -euo pipefail

MIN_VERSION_MAJOR=10
MIN_VERSION_MINOR=22
BIN_PATH=/usr/local/bin/azcopy

if [[ -x "$BIN_PATH" ]]; then
    # Parse "azcopy version 10.22.1" → "10.22.1"
    current=$("$BIN_PATH" --version 2>/dev/null | awk '/azcopy version/ {print $3}' || true)
    if [[ -n "$current" ]]; then
        major=$(echo "$current" | cut -d. -f1)
        minor=$(echo "$current" | cut -d. -f2)
        if (( major > MIN_VERSION_MAJOR )) || (( major == MIN_VERSION_MAJOR && minor >= MIN_VERSION_MINOR )); then
            echo "azcopy $current already installed; skipping."
            exit 0
        fi
        echo "azcopy $current is older than ${MIN_VERSION_MAJOR}.${MIN_VERSION_MINOR}; upgrading."
    fi
fi

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

echo "Downloading azcopy v10 latest…"
curl -fsSL "https://aka.ms/downloadazcopy-v10-linux" -o "$tmpdir/azcopy.tar.gz"
tar -xz -C "$tmpdir" -f "$tmpdir/azcopy.tar.gz"

# Tarball expands to azcopy_linux_amd64_*/azcopy
binary=$(find "$tmpdir" -name azcopy -type f -executable | head -1)
if [[ -z "$binary" ]]; then
    echo "ERROR: azcopy binary not found in tarball" >&2
    exit 1
fi

install -m 0755 "$binary" "$BIN_PATH"
echo "Installed: $("$BIN_PATH" --version)"
