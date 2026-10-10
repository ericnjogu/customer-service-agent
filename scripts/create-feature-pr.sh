#!/usr/bin/env bash
# Usage: bash scripts/create-feature-pr.sh BRANCH TITLE BODY_FILE FILE [FILE...]
# Uses gh's authenticated identity (or GH_TOKEN, e.g. a GitHub App installation
# token). Never place a token in arguments. Only explicitly listed files commit.
# Branches from current HEAD: update to the desired base before invoking.
set -euo pipefail
if [[ $# -lt 4 ]]; then
  echo "Usage: $0 BRANCH TITLE BODY_FILE FILE [FILE...]" >&2
  exit 2
fi
branch="$1"; title="$2"; body_file="$3"; shift 3
[[ -f "$body_file" ]] || { echo "PR body file not found" >&2; exit 1; }
body_file="$(cd "$(dirname "$body_file")" && pwd)/$(basename "$body_file")"
cd "$(git rev-parse --show-toplevel)"
git check-ref-format --branch "$branch" >/dev/null
git diff --cached --quiet || { echo "Unstage existing changes first." >&2; exit 1; }
gh auth status >/dev/null
bash scripts/install-git-hooks.sh
git switch -c "$branch"
git add -- "$@"
git diff --cached --check
git diff --cached --quiet && { echo "No selected changes to commit." >&2; exit 1; }
git commit -m "$title"
# The pre-push hook runs both browser QA suites; never bypass it.
git push --set-upstream origin "$branch"
gh pr create --base main --head "$branch" --title "$title" --body-file "$body_file"
# On failure, preserve the branch/commit for inspection; do not reset or delete.
