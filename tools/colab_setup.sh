#!/usr/bin/env bash
# First setup populates a Drive cache; subsequent setups try offline installation first.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
clipforge_default_profile=gpu
if [[ ${CLIPFORGE_CPU_ONLY:-0} == 1 ]]; then clipforge_default_profile=cpu; fi
export CLIPFORGE_CACHE_PROFILE=${CLIPFORGE_CACHE_PROFILE-$clipforge_default_profile}
case "$CLIPFORGE_CACHE_PROFILE" in
  cpu|gpu) ;;
  *) echo 'Invalid cache profile: use cpu or gpu.' >&2; exit 1 ;;
esac
clipforge_timings=${CLIPFORGE_SETUP_TIMINGS:-}
clipforge_record_timing() {
  [[ -n $clipforge_timings ]] || return 0
  awk -v name="$1" -v started="$2" -v finished="$(date +%s.%N)" \
    'BEGIN { printf "%s\t%.3f\n", name, finished - started }' >> "$clipforge_timings"
}
clipforge_should_save() {
  [[ ${1:-0} == 1 || ${2:-0} == 1 ]]
}
fetched=0
if [[ ${CLIPFORGE_CPU_ONLY:-0} != 1 ]]; then nvidia-smi; fi
source tools/colab_cache_env.sh
cache_restore_started=$(date +%s.%N)
python3 tools/colab_cache.py restore
clipforge_record_timing cache_restore "$cache_restore_started"
os_deps_started=$(date +%s.%N)
bash tools/colab_os_deps.sh
clipforge_record_timing os_deps "$os_deps_started"
uv_bootstrap_started=$(date +%s.%N)
if [[ $(uv --version 2>/dev/null || true) != 'uv 0.12.23'* ]]; then
  mkdir -p "$CLIPFORGE_CACHE_ROOT/bootstrap"
  uv_wheels=("$CLIPFORGE_CACHE_ROOT/bootstrap"/uv-0.12.23-*.whl)
  if [[ ! -f ${uv_wheels[0]} ]]; then
    if [[ ${CLIPFORGE_OFFLINE:-0} == 1 ]]; then
      echo 'uv bootstrap wheel is not cached; offline mode forbids downloading.' >&2
      exit 1
    fi
    python3 -m pip download --only-binary=:all: --no-deps --dest "$CLIPFORGE_CACHE_ROOT/bootstrap" 'uv==0.12.23'
    fetched=1
  fi
  python3 -m pip install --no-index --no-deps --find-links "$CLIPFORGE_CACHE_ROOT/bootstrap" 'uv==0.12.23'
fi
clipforge_record_timing uv_bootstrap "$uv_bootstrap_started"
uv_sync_started=$(date +%s.%N)
uv_sync_args=(--locked --python 3.11 --extra chat --extra media-fetch --no-dev)
if [[ $CLIPFORGE_CACHE_PROFILE == gpu ]]; then uv_sync_args+=(--extra cuda); fi
if uv sync --offline "${uv_sync_args[@]}"; then
  echo 'Python dependencies installed from the local cache; no package download required.'
else
  if [[ ${CLIPFORGE_OFFLINE:-0} == 1 ]]; then
    echo 'Python dependencies are not fully cached; offline mode forbids downloading.' >&2
    exit 1
  fi
  uv sync "${uv_sync_args[@]}"
  fetched=1
fi
clipforge_record_timing uv_sync "$uv_sync_started"
cuda_check_started=$(date +%s.%N)
if [[ ${CLIPFORGE_CPU_ONLY:-0} != 1 ]]; then
  source tools/colab_cuda_env.sh
  .venv/bin/python - <<'PY'
import ctypes
import ctranslate2
ctypes.CDLL('libcublas.so.12')
ctypes.CDLL('libcudnn.so.9')
if ctranslate2.get_cuda_device_count() < 1:
    raise SystemExit('No CUDA device: select a GPU runtime in Colab.')
if 'float16' not in ctranslate2.get_supported_compute_types('cuda'):
    raise SystemExit('CUDA float16 is unavailable.')
print('CUDA libraries and CTranslate2 GPU/float16 capability detected; ASR not run yet.')
PY
fi
clipforge_record_timing cuda_check "$cuda_check_started"
# Model files only: no inference or Gemini API call. The requested model is unchanged.
model_prepare_started=$(date +%s.%N)
if [[ $CLIPFORGE_CACHE_PROFILE == gpu && ${CLIPFORGE_SKIP_MODEL_PREPARE:-0} != 1 ]]; then
  if ! HF_HUB_OFFLINE=1 .venv/bin/python - >/dev/null 2>&1 <<'PY'
import os
from clipforge.whisper_cache import model_path
model_path(os.environ.get('CLIPFORGE_WHISPER_MODEL', 'small'), os.environ.get('CLIPFORGE_WHISPER_REVISION'))
PY
  then
    fetched=1
  fi
  .venv/bin/python - <<'PY'
import os
from clipforge.whisper_cache import model_path
try:
    model_path(os.environ.get('CLIPFORGE_WHISPER_MODEL', 'small'), os.environ.get('CLIPFORGE_WHISPER_REVISION'))
except Exception as error:
    raise SystemExit('Whisper asset preparation failed: ' + type(error).__name__) from None
print('Requested Whisper model files are available; inference has not been run.')
PY
fi
clipforge_record_timing model_prepare "$model_prepare_started"
runner_asset_started=$(date +%s.%N)
if [[ ${CLIPFORGE_SKIP_RUNNER_ASSET:-0} != 1 ]]; then
  python3 tools/colab_cache.py runner >/dev/null
fi
clipforge_record_timing runner_asset "$runner_asset_started"
# Tell the worker that this setup fetched something the shared cache does not hold yet.
if [[ $fetched == 1 && -n ${CLIPFORGE_SETUP_FETCHED:-} ]]; then : > "$CLIPFORGE_SETUP_FETCHED"; fi
if clipforge_should_save "${CLIPFORGE_PREPARE_ONLY:-0}" "$fetched"; then
  cache_save_started=$(date +%s.%N)
  python3 tools/colab_cache.py save
  clipforge_record_timing cache_save "$cache_save_started"
else
  echo 'Dependency snapshot unchanged; save skipped.'
fi
if [[ ${CLIPFORGE_PREPARE_ONLY:-0} == 1 ]]; then
  echo 'Public assets prepared without requiring a GPU. No runner registered; no inference performed.'
fi
