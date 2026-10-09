#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
existing="$(git config --get core.hooksPath || true)"
if [[ -n "$existing" && "$existing" != ".githooks" ]]; then
  echo "Existing hooks path '$existing' retained. Integrate .githooks/pre-push manually." >&2
  exit 1
fi
if [[ -z "$existing" && -f "$(git rev-parse --git-path hooks/pre-push)" ]]; then
  echo "Existing pre-push hook retained. Integrate .githooks/pre-push manually." >&2
  exit 1
fi
git config --local core.hooksPath .githooks
echo "Installed pre-push browser QA: Telegram and WhatsApp must both pass."
