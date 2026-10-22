#!/usr/bin/env bash
# Install into this checkout, without copying Colab's system Python packages.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
nvidia-smi
if [[ $(id -u) == 0 ]]; then
  apt=(apt-get)
else
  apt=(sudo apt-get)
fi
"${apt[@]}" update -qq
"${apt[@]}" install -y --no-install-recommends ffmpeg fontconfig fonts-noto-cjk curl git libicu-dev
if ! command -v uv >/dev/null; then
  python3 -m pip install 'uv==0.12.23'
fi
uv sync --locked --python 3.11 --extra chat --extra cuda --no-dev

# Export in the caller too: source tools/colab_cuda_env.sh
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
