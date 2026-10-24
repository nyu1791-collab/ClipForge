#!/usr/bin/env bash
# Colab setup for clip rendering: OS tools, uv and the locked project environment.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
clipforge_timings=${CLIPFORGE_SETUP_TIMINGS:-}
clipforge_record_timing() {
  [[ -n $clipforge_timings ]] || return 0
  awk -v name="$1" -v started="$2" -v finished="$(date +%s.%N)" \
    'BEGIN { printf "%s\t%.3f\n", name, finished - started }' >> "$clipforge_timings"
}
source tools/colab_cache_env.sh
bash tools/ensure_node.sh
os_deps_started=$(date +%s.%N)
bash tools/colab_os_deps.sh
clipforge_record_timing os_deps "$os_deps_started"
uv_bootstrap_started=$(date +%s.%N)
if [[ $(uv --version 2>/dev/null || true) != 'uv 0.12.23'* ]]; then
  mkdir -p "$CLIPFORGE_CACHE_ROOT/bootstrap"
  uv_wheels=("$CLIPFORGE_CACHE_ROOT/bootstrap"/uv-0.12.23-*.whl)
  if [[ ! -f ${uv_wheels[0]} ]]; then
    python3 -m pip download --only-binary=:all: --no-deps --dest "$CLIPFORGE_CACHE_ROOT/bootstrap" 'uv==0.12.23'
  fi
  python3 -m pip install --no-index --no-deps --find-links "$CLIPFORGE_CACHE_ROOT/bootstrap" 'uv==0.12.23'
fi
clipforge_record_timing uv_bootstrap "$uv_bootstrap_started"
uv_sync_started=$(date +%s.%N)
uv_sync_args=(--locked --python 3.11 --extra chat --extra media-fetch --no-dev)
if uv sync --offline "${uv_sync_args[@]}"; then
  echo 'Python dependencies installed from the local cache; no package download required.'
else
  uv sync "${uv_sync_args[@]}"
fi
clipforge_record_timing uv_sync "$uv_sync_started"
