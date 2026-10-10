#!/usr/bin/env bash
# Usage: bash scripts/create-feature-pr.sh BRANCH
# Uses gh's authenticated identity (or GH_TOKEN, e.g. a GitHub App installation
# token). Never place a token in arguments. Includes ALL staged/unstaged changes
# and non-ignored untracked files. Review git status for sensitive files first.
set -euo pipefail

# An old Intel-only Git can precede the working system/Homebrew Git on PATH.
# Export the selected directory so child scripts, hooks, and gh use it too.
select_git() {
  local candidate
  for candidate in "$(command -v git || true)" /opt/homebrew/bin/git /usr/bin/git; do
    if [[ -n "$candidate" && -x "$candidate" ]] &&
        ("$candidate" --version) >/dev/null 2>&1; then
      export PATH="$(dirname "$candidate"):$PATH"
      hash -r
      return
    fi
  done
  echo "No working Git installation found. Install Git before continuing." >&2
  exit 1
}

# Load the function before switching branches (which may replace this script).
main() {
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 BRANCH" >&2
  exit 2
fi
branch="$1"
select_git
cd "$(git rev-parse --show-toplevel)"
git check-ref-format --branch "$branch" >/dev/null
if [[ "$branch" == "main" ]] || git show-ref --verify --quiet "refs/heads/$branch"; then
  echo "Choose a new feature branch name." >&2; exit 1
fi
[[ -z "$(git ls-files -u)" ]] || { echo "Resolve existing conflicts first." >&2; exit 1; }
[[ -n "$(git status --porcelain)" ]] || { echo "No changes to commit." >&2; exit 1; }
gh auth status >/dev/null

# Keep the draft outside the working tree so it is never included by git add -A.
# Git chooses GIT_EDITOR, core.editor, VISUAL, EDITOR, then its default editor.
draft="$(git rev-parse --absolute-git-dir)/FEATURE_PR_EDITMSG"
cp scripts/feature-pr-template.txt "$draft"
editor="$(git var GIT_EDITOR)"
if ! sh -c "$editor \"\$1\"" -- "$draft"; then
  echo "Editor cancelled. No branch or commit created. Draft: $draft" >&2
  exit 1
fi
# Only #: lines are guidance; ordinary Markdown headings remain in the body.
title="$(awk '!/^#:/ && /[^[:space:]]/ {sub(/\r$/, ""); print; exit}' "$draft")"
body="$(awk '/^#:/ {next} !found {if (/[^[:space:]]/) found=1; next} {print}' "$draft")"
if [[ -z "$title" || -z "$(printf '%s' "$body" | tr -d '[:space:]')" ]]; then
  echo "A title and description are required. No branch or commit created. Draft: $draft" >&2
  exit 1
fi
bash scripts/install-git-hooks.sh

# Fetch before touching the working tree. Never reset local main or force-push.
git fetch origin
git stash push --include-untracked -m "create-feature-pr: $branch"
saved_stash="$(git rev-parse refs/stash)"
echo "Saved changes as stash commit $saved_stash (retained if restoration fails)."
if git show-ref --verify --quiet refs/heads/main; then
  git switch main
else
  git switch --track -c main origin/main
fi
git pull --ff-only origin main
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  echo "Local main has unpublished commits. Stash $saved_stash retained; reconcile main first." >&2
  exit 1
fi
git switch -c "$branch"
if ! git stash apply --index "$saved_stash"; then
  echo "Restore conflict: resolve manually. Original changes remain in stash $saved_stash." >&2
  exit 1
fi
# Drop only our stash, and only if it is still the top entry and was restored.
if [[ "$(git rev-parse refs/stash)" == "$saved_stash" ]]; then
  git stash drop 'stash@{0}'
fi
git add --all
git diff --cached --check
git diff --cached --quiet && { echo "No changes to commit after updating main." >&2; exit 1; }
git commit -m "$title"
# The pre-push hook runs both browser QA suites; never bypass it.
git push --set-upstream origin "$branch"
gh pr create --base main --head "$branch" --title "$title" --body "$body"
# On failure, preserve the branch/commit for inspection; do not reset or delete.
}
main "$@"
