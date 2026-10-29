#!/usr/bin/env bash
# First-time setup for Ubuntu, including Ubuntu on WSL2. No production runs.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
  cat <<'EOF'
Usage: bash setup.sh [--dev] [--no-system]

Default: install missing Ubuntu system packages and uv if needed, then install
the locked runtime dependencies. sudo may ask for your password. Network access
is required for installation. Nothing is dispatched or authenticated.
  --dev        Also install the existing development and media dependencies.
  --no-system  Do not install OS packages; report anything missing instead.
  -h, --help   Show this message without changing the system.
See docs/PC_MIGRATION.md for Windows and WSL2 setup.
EOF
}

dev=0
install_system=1
for arg in "$@"; do
  case "$arg" in
    --dev) dev=1 ;;
    --no-system) install_system=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $(uname -s) != Linux ]] || ! command -v apt-get >/dev/null || ! command -v dpkg-query >/dev/null; then
  echo 'Use Ubuntu on WSL2 or Debian/Ubuntu Linux. See docs/PC_MIGRATION.md.' >&2
  exit 1
fi

missing=()
for package in git curl ca-certificates ffmpeg fontconfig fonts-noto-cjk libcairo2 gh; do
  if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then
    missing+=("$package")
  fi
done
if (( ${#missing[@]} )); then
  if (( ! install_system )); then
    printf 'Missing OS packages: %s\n' "${missing[*]}" >&2
    echo 'Install them or run bash setup.sh without --no-system.' >&2
    exit 1
  fi
  privilege=()
  if (( EUID != 0 )); then
    if ! command -v sudo >/dev/null; then
      echo 'sudo is required to install missing OS packages.' >&2
      exit 1
    fi
    privilege=(sudo)
  fi
  printf 'Installing missing OS packages: %s\n' "${missing[*]}"
  "${privilege[@]}" apt-get update
  "${privilege[@]}" apt-get install -y --no-install-recommends "${missing[@]}"
fi

# Prefer an existing uv installation. The fallback is the official versioned
# installer, kept outside the repository and not added to shell profiles.
export PATH="${HOME}/.local/bin:${PATH}"
if ! command -v uv >/dev/null; then
  if (( ! install_system )); then
    echo 'uv is missing; install it or run bash setup.sh without --no-system.' >&2
    exit 1
  fi
  installer=$(mktemp "${TMPDIR:-/tmp}/clipforge-uv-installer.XXXXXXXX")
  trap 'rm -f -- "$installer"' EXIT
  echo 'Installing uv 0.12.23 from astral.sh into ~/.local/bin.'
  curl --fail --location --silent --show-error \
    https://astral.sh/uv/0.12.23/install.sh -o "$installer"
  UV_INSTALL_DIR="${HOME}/.local/bin" UV_NO_MODIFY_PATH=1 sh "$installer"
fi
if ! command -v uv >/dev/null; then
  echo 'uv installation did not make uv available. See docs/PC_MIGRATION.md.' >&2
  exit 1
fi

python_option=()
if [[ ! -e .venv ]]; then
  # The production Actions and optional Colab control dependencies use 3.12.
  # Do not force a Python version change on an existing environment.
  python_option=(--python 3.12)
fi
if (( dev )); then
  bash tools/dev_setup.sh "${python_option[@]}"
else
  uv sync --locked --no-dev --inexact "${python_option[@]}"
fi

printf '\nSetup finished. Current code:\n'
git status --short --branch
git rev-parse HEAD
echo 'No model requests, Actions runs, video rendering, tests, or logins were started.'
echo 'If uv is not found in a new terminal: export PATH="$HOME/.local/bin:$PATH"'
echo 'GitHub login and existing production data are separate; see docs/PC_MIGRATION.md.'
