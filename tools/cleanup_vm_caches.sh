#!/usr/bin/env bash
# Run through the VM owner's normal administrative SSH connection.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run this script using the VM owner administrative connection. Restricted MCP cannot acquire root.' >&2
  exit 1
fi
df -h /
# Package archives can be downloaded again; no installed packages are removed.
apt-get clean
# Only named build/download caches, and only after seven days without changes.
for cache in /tmp/clipforge-ruff-0.14.14 /tmp/clipforge-cleanup-deps /tmp/node-compile-cache /tmp/gh-cli-cache; do
  if [[ -d "$cache" && ! -L "$cache" ]] && ! find "$cache" -type f -mmin -10080 -print -quit | read -r recent; then
    rm -rf -- "$cache"
  fi
done
df -h /
