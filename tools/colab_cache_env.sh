#!/usr/bin/env bash
# Values are shell-quoted by the repository's standard-library-only cache helper.
clipforge_cache_script=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/colab_cache.py
clipforge_cache_exports=$(python3 "$clipforge_cache_script" env) || return 1
eval "$clipforge_cache_exports"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export CLIPFORGE_NODE_ROOT="$CLIPFORGE_CACHE_ROOT/node-v22.23.4"
if [[ -x "$CLIPFORGE_NODE_ROOT/bin/node" ]]; then
  export PATH="$CLIPFORGE_NODE_ROOT/bin:$PATH"
fi
unset clipforge_cache_script clipforge_cache_exports
