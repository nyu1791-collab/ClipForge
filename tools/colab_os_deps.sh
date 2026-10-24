#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source tools/colab_cache_env.sh
missing=()
packages=(ffmpeg fontconfig fonts-noto-cjk git gh)
for package in "${packages[@]}"; do
  if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
    missing+=("$package")
  fi
done
if [[ ${#missing[@]} == 0 ]]; then
  echo 'OS dependencies already installed; apt update and download skipped.'
  exit 0
fi
if [[ $(id -u) == 0 ]]; then apt=(apt-get); else apt=(sudo apt-get); fi
mkdir -p "$CLIPFORGE_CACHE_ROOT/apt/partial"
cache_options=(-o "Dir::Cache::archives=$CLIPFORGE_CACHE_ROOT/apt" -o APT::Keep-Downloaded-Packages=true)
if "${apt[@]}" "${cache_options[@]}" --no-download install -y --no-install-recommends "${missing[@]}" \
  > "$CLIPFORGE_CACHE_ROOT/apt/offline-install.log" 2>&1; then
  echo 'OS dependencies installed from cached packages.'
else
  "${apt[@]}" update -qq
  "${apt[@]}" "${cache_options[@]}" install -y --no-install-recommends "${missing[@]}"
fi
