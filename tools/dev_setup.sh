#!/usr/bin/env bash
# Reproducible development setup for Claude Code and fresh cloud sessions.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v uv >/dev/null; then
  echo 'uv is required: install uv, then rerun bash tools/dev_setup.sh.' >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null || ! command -v fc-match >/dev/null || \
   ! fc-match 'Noto Sans CJK JP' --format '%{family}' | grep -q 'Noto Sans CJK JP'; then
  if ! command -v apt-get >/dev/null; then
    echo 'Install ffmpeg, fontconfig and Noto Sans CJK JP, then rerun this script.' >&2
    exit 1
  fi
  privilege=()
  if [[ $(id -u) != 0 ]]; then
    if ! command -v sudo >/dev/null || ! sudo -n true; then
      echo 'Install ffmpeg fontconfig fonts-noto-cjk with administrator access, then rerun.' >&2
      exit 1
    fi
    privilege=(sudo -n)
  fi
  "${privilege[@]}" apt-get update -qq
  "${privilege[@]}" apt-get install -y ffmpeg fontconfig fonts-noto-cjk
fi

# The controller does not run GPU inference. CUDA is installed only when requested.
uv sync --locked --group dev --extra chat --extra media-fetch "$@"
uv run --no-sync python -c 'from clipforge.fonts import resolve; print(resolve("Noto Sans CJK JP"))'
uv run --no-sync python -m pytest --version
uv run --no-sync ruff --version
echo 'Development environment ready; tests and video generation have not been run.'
