#!/usr/bin/env bash
# A session runner accepts jobs until Colab ends the runtime. No keep-alive tricks.
set -euo pipefail
set +x
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -d /content ]] || { echo 'Run this script inside a Colab GPU runtime.' >&2; exit 1; }
nvidia-smi
source tools/colab_cache_env.sh
source tools/colab_cuda_env.sh
export RUNNER_ALLOW_RUNASROOT=1
export CLIPFORGE_REQUIRE_CUDA=1
mode=${COLAB_RUNNER_MODE:-session}
case "$mode" in
  session) runner_options=() ;;
  ephemeral) runner_options=(--ephemeral) ;;
  *) echo 'COLAB_RUNNER_MODE must be session or ephemeral.' >&2; exit 1 ;;
esac
# The archive is cached separately from the temporary runner's credentials/workspace.
runner_archive=$(python3 tools/colab_cache.py runner)
runner_url=https://github.com/nyu1791-collab/ai-model-workers
runner_dir=$(mktemp -d /content/clipforge-runner.XXXXXX)
trap 'rm -rf -- "$runner_dir"' EXIT
cd "$runner_dir"
tar xzf "$runner_archive"
if [[ -z ${RUNNER_TOKEN:-} ]]; then
  read -r -s -p 'Private worker registration token: ' RUNNER_TOKEN
  echo
fi
[[ -n ${RUNNER_TOKEN:-} ]] || { echo 'Runner registration token is required.' >&2; exit 1; }
./config.sh --url "$runner_url" --token "$RUNNER_TOKEN" \
  --name "colab-t4-${runner_dir##*.}" --labels colab-gpu \
  --work _work --unattended "${runner_options[@]}"
unset RUNNER_TOKEN GH_TOKEN GITHUB_TOKEN
if [[ $mode == session ]]; then
  echo 'Accepting private jobs automatically while this Colab GPU session remains connected.'
else
  echo 'Waiting for one job; this runner deregisters when the job finishes.'
fi
./run.sh
