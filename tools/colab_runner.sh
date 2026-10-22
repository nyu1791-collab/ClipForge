#!/usr/bin/env bash
# Run one trusted private GitHub Actions job, then exit and deregister.
set -euo pipefail
set +x
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -d /content ]] || { echo 'Run this script inside a Colab GPU runtime.' >&2; exit 1; }
nvidia-smi
source tools/colab_cuda_env.sh
export RUNNER_ALLOW_RUNASROOT=1
export CLIPFORGE_REQUIRE_CUDA=1
runner_url=https://github.com/nyu1791-collab/ai-model-workers
runner_dir=$(mktemp -d /content/clipforge-runner.XXXXXX)
trap 'rm -rf -- "$runner_dir"' EXIT
export CLIPFORGE_RUNNER_DOWNLOAD_DIR="$runner_dir"

# Use GitHub release metadata and verify the official asset SHA-256 before extraction.
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

root = Path(os.environ['CLIPFORGE_RUNNER_DOWNLOAD_DIR'])
with urllib.request.urlopen('https://api.github.com/repos/actions/runner/releases/latest', timeout=60) as response:
    release = json.load(response)
version = release['tag_name'].removeprefix('v')
name = f'actions-runner-linux-x64-{version}.tar.gz'
asset = next(a for a in release['assets'] if a['name'] == name)
checksum = (asset.get('digest') or '').removeprefix('sha256:')
if not re.fullmatch(r'[a-fA-F0-9]{64}', checksum):
    # Older releases put their per-platform SHA in the release notes.
    match = re.search(r'<!-- BEGIN SHA linux-x64 -->\s*([a-fA-F0-9]{64})', release.get('body', ''))
    if not match:
        raise SystemExit('Official runner checksum unavailable; refusing unverified download.')
    checksum = match[1]
url = asset['browser_download_url']
if not url.startswith('https://github.com/actions/runner/releases/download/'):
    raise SystemExit('Unexpected runner download URL.')
sha = hashlib.sha256()
with urllib.request.urlopen(url, timeout=120) as response, (root / 'runner.tar.gz').open('xb') as target:
    while data := response.read(1024 * 1024):
        sha.update(data)
        target.write(data)
if sha.hexdigest().lower() != checksum.lower():
    raise SystemExit('Runner SHA-256 mismatch.')
print('Verified official GitHub Actions runner', version)
PY
unset CLIPFORGE_RUNNER_DOWNLOAD_DIR
cd "$runner_dir"
tar xzf runner.tar.gz
rm runner.tar.gz
if [[ -z ${RUNNER_TOKEN:-} ]]; then
  read -r -s -p 'Private worker registration token: ' RUNNER_TOKEN
  echo
fi
[[ -n ${RUNNER_TOKEN:-} ]] || { echo 'Runner registration token is required.' >&2; exit 1; }
# Never replace another runner. --ephemeral really deregisters after one job.
./config.sh --url "$runner_url" --token "$RUNNER_TOKEN" \
  --name "colab-t4-${runner_dir##*.}" --labels colab-gpu \
  --work _work --unattended --ephemeral
unset RUNNER_TOKEN GH_TOKEN GITHUB_TOKEN
echo 'Waiting for one job from the private ai-model-workers repository.'
./run.sh
