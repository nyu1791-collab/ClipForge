#!/usr/bin/env bash
# First-time setup for Ubuntu, including Ubuntu on WSL2. No production runs.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
  cat <<'EOF'
使い方: bash setup.sh [--dev] [--no-system]

Ubuntu の不足パッケージと uv を必要に応じて導入し、固定された Python 依存を入れます。
ネット接続が必要です。sudo のパスワードを求められる場合があります。
制作の投入、ログイン、描画は行いません。
  --dev        開発用とメディア取得用の依存も導入する
  --no-system  OS パッケージと uv の自動導入をせず、不足時に終了する
  -h, --help   変更せず、この説明を表示する
Windows と WSL2 の手順: docs/PC_MIGRATION.md
EOF
}

dev=0
install_system=1
for arg in "$@"; do
  case "$arg" in
    --dev) dev=1 ;;
    --no-system) install_system=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "不明なオプション: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $(uname -s) != Linux ]] || ! command -v apt-get >/dev/null || ! command -v dpkg-query >/dev/null; then
  echo 'Ubuntu on WSL2 または Debian/Ubuntu Linux で実行してください。docs/PC_MIGRATION.md を参照。' >&2
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
    printf '不足している OS パッケージ: %s\n' "${missing[*]}" >&2
    echo 'インストールするか、--no-system を外して再実行してください。' >&2
    exit 1
  fi
  privilege=()
  if (( EUID != 0 )); then
    if ! command -v sudo >/dev/null; then
      echo '不足している OS パッケージを導入するには sudo が必要です。' >&2
      exit 1
    fi
    privilege=(sudo)
  fi
  printf '不足している OS パッケージをインストールします: %s\n' "${missing[*]}"
  "${privilege[@]}" apt-get update
  "${privilege[@]}" apt-get install -y --no-install-recommends "${missing[@]}"
fi

# Prefer an existing uv installation. The fallback is the official versioned
# installer, kept outside the repository and not added to shell profiles.
export PATH="${HOME}/.local/bin:${PATH}"
if ! command -v uv >/dev/null; then
  if (( ! install_system )); then
    echo 'uv がありません。インストールするか、--no-system を外して再実行してください。' >&2
    exit 1
  fi
  installer=$(mktemp "${TMPDIR:-/tmp}/clipforge-uv-installer.XXXXXXXX")
  trap 'rm -f -- "$installer"' EXIT
  echo '公式 astral.sh から uv 0.12.23 を ~/.local/bin にインストールします。'
  curl --fail --location --silent --show-error \
    https://astral.sh/uv/0.12.23/install.sh -o "$installer"
  UV_INSTALL_DIR="${HOME}/.local/bin" UV_NO_MODIFY_PATH=1 sh "$installer"
fi
if ! command -v uv >/dev/null; then
  echo 'uv を使用できません。docs/PC_MIGRATION.md を参照してください。' >&2
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

printf '\nセットアップ完了。現在のコード:\n'
git status --short --branch
git rev-parse HEAD
echo 'モデル要求・Actions・描画・テスト・ログインは実行していません。'
echo '新しいターミナルで uv が見つからない場合: export PATH="$HOME/.local/bin:$PATH"'
echo 'GitHub ログインと既存の制作データは別途必要です。docs/PC_MIGRATION.md を参照。'
