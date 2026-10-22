#!/usr/bin/env bash
# The CPU video worker needs a supported JS runtime for yt-dlp's bundled YouTube solver.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/colab_cache_env.sh"
if command -v node >/dev/null && node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
  exit 0
fi
if [[ ${CLIPFORGE_OFFLINE:-0} == 1 ]]; then
  echo 'Node.js 22+ is not cached; offline mode forbids downloading.' >&2
  exit 1
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'Install Node.js 22+ for this worker architecture.' >&2
  exit 1
fi
clipforge_node_staging=$(mktemp -d "$CLIPFORGE_CACHE_ROOT/.node-XXXXXX")
trap 'rm -rf "$clipforge_node_staging"' EXIT
python3 - "$clipforge_node_staging/node.tar.xz" <<'PY'
import hashlib, pathlib, sys, urllib.request
target = pathlib.Path(sys.argv[1])
url = 'https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-x64.tar.xz'
checksum = hashlib.sha256()
total = 0
with urllib.request.urlopen(url, timeout=30) as source, target.open('xb') as output:
    while block := source.read(1024 * 1024):
        total += len(block)
        if total > 100 * 1024**2:
            raise SystemExit('Node.js archive exceeds its size limit.')
        checksum.update(block)
        output.write(block)
if checksum.hexdigest() != '1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce':
    raise SystemExit('Node.js archive checksum mismatch.')
PY
mkdir "$clipforge_node_staging/runtime"
tar -xJf "$clipforge_node_staging/node.tar.xz" --strip-components=1 -C "$clipforge_node_staging/runtime"
"$clipforge_node_staging/runtime/bin/node" --version
if [[ -e "$CLIPFORGE_NODE_ROOT" ]]; then
  echo 'Existing Node.js cache is unusable; refusing to overwrite it.' >&2
  exit 1
fi
mv "$clipforge_node_staging/runtime" "$CLIPFORGE_NODE_ROOT"
