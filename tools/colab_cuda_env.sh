#!/usr/bin/env bash
# Source this before launching Python: the loader needs these paths at startup.
clipforge_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
clipforge_cuda_libs=$("$clipforge_root/.venv/bin/python" - <<'PY'
from pathlib import Path
import site
directories = []
for directory in site.getsitepackages():
    directories.extend(str(p) for p in sorted(Path(directory).glob('nvidia/*/lib')))
if not directories:
    raise SystemExit('CUDA extra is not installed; run tools/colab_setup.sh first.')
print(':'.join(directories))
PY
)
export LD_LIBRARY_PATH="$clipforge_cuda_libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
unset clipforge_root clipforge_cuda_libs
