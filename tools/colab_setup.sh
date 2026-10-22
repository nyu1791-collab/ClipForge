#!/usr/bin/env bash
# First setup populates a Drive cache; subsequent setups try offline installation first.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ ${CLIPFORGE_PREPARE_ONLY:-0} != 1 ]]; then nvidia-smi; fi
source tools/colab_cache_env.sh
python3 tools/colab_cache.py restore
bash tools/colab_os_deps.sh
if [[ $(uv --version 2>/dev/null || true) != 'uv 0.12.23'* ]]; then
  mkdir -p "$CLIPFORGE_CACHE_ROOT/bootstrap"
  uv_wheels=("$CLIPFORGE_CACHE_ROOT/bootstrap"/uv-0.12.23-*.whl)
  if [[ ! -f ${uv_wheels[0]} ]]; then
    if [[ ${CLIPFORGE_OFFLINE:-0} == 1 ]]; then
      echo 'uv bootstrap wheel is not cached; offline mode forbids downloading.' >&2
      exit 1
    fi
    python3 -m pip download --only-binary=:all: --no-deps --dest "$CLIPFORGE_CACHE_ROOT/bootstrap" 'uv==0.12.23'
  fi
  python3 -m pip install --no-index --no-deps --find-links "$CLIPFORGE_CACHE_ROOT/bootstrap" 'uv==0.12.23'
fi
if uv sync --offline --locked --python 3.11 --extra chat --extra cuda --no-dev; then
  echo 'Python dependencies installed from the local cache; no package download required.'
else
  if [[ ${CLIPFORGE_OFFLINE:-0} == 1 ]]; then
    echo 'Python dependencies are not fully cached; offline mode forbids downloading.' >&2
    exit 1
  fi
  uv sync --locked --python 3.11 --extra chat --extra cuda --no-dev
fi
if [[ ${CLIPFORGE_PREPARE_ONLY:-0} != 1 ]]; then
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
# Model files only: no inference or Gemini API call. The requested model is unchanged.
if [[ ${CLIPFORGE_SKIP_MODEL_PREPARE:-0} != 1 ]]; then
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
if [[ ${CLIPFORGE_SKIP_RUNNER_ASSET:-0} != 1 ]]; then
  python3 tools/colab_cache.py runner >/dev/null
fi
python3 tools/colab_cache.py save
if [[ ${CLIPFORGE_PREPARE_ONLY:-0} == 1 ]]; then
  echo 'Public assets prepared without requiring a GPU. No runner registered; no inference performed.'
fi
