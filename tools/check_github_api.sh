#!/usr/bin/env bash
# Report whether the GitHub API path used by the ClipForge tools (gh api) works.
# Uses gh api, not gh auth status: auth status can call a token invalid while the
# API calls the tools make still succeed. Reports only; always exits 0.
login=$(gh api user --jq .login 2>/dev/null) &&
  echo "GitHub API: ok (login=${login})" ||
  echo "GitHub API: failed. Reconnect GitHub at https://claude.ai/connect-github before production."
gh api repos/nyu1791-collab/ai-model-workers --jq '.full_name' >/dev/null 2>&1 &&
  echo "GitHub API: private store nyu1791-collab/ai-model-workers readable" ||
  echo "GitHub API: private store nyu1791-collab/ai-model-workers not readable"
exit 0
