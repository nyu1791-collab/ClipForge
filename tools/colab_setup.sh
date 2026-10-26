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
# OS tools and Python wheels have independent prerequisites. YouTube's yt-dlp
# transport can require Node; skip its setup only for known non-YouTube inputs.
(
  os_deps_started=$(date +%s.%N)
  bash tools/colab_os_deps.sh
  clipforge_record_timing os_deps "$os_deps_started"
  clipforge_source_url=${CLIPFORGE_SOURCE_URL:-}
  case "${clipforge_source_url,,}" in
    ''|*youtube.com/*|*youtu.be/*|*youtube-nocookie.com/*)
      node_started=$(date +%s.%N)
      bash tools/ensure_node.sh
      clipforge_record_timing node "$node_started"
      ;;
  esac
) &
clipforge_os_pid=$!
trap 'wait "$clipforge_os_pid" || true' EXIT
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
uv_sync_args=(--locked --python 3.11 --extra media-fetch --no-dev)
clipforge_chat_enabled=$(python3 - <<'PYCODE'
import json,os,pathlib
config=pathlib.Path(os.environ.get('CLIPFORGE_RENDER_CONFIG','config.landscape.example.json'))
print('1' if json.loads(config.read_text()).get('chat_panel',False) else '0')
PYCODE
)
if [[ $clipforge_chat_enabled == 1 ]]; then uv_sync_args+=(--extra chat); fi
if uv sync --offline "${uv_sync_args[@]}"; then
  echo 'Python dependencies installed from the local cache; no package download required.'
else
  uv sync "${uv_sync_args[@]}"
fi
clipforge_record_timing uv_sync "$uv_sync_started"

wait "$clipforge_os_pid"
trap - EXIT
