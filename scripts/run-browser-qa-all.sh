#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for channel in telegram whatsapp; do
  echo "Running $channel browser QA..."
  AGENT_E2E_CHANNEL="$channel" bash "$repo_dir/scripts/run-browser-qa.sh" "$@"
done
