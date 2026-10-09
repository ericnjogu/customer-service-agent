# Project guidance for Codex agents

## Dependency policy

- Prefer established libraries over custom code.
- Before adding a dependency, check that it has recent releases and no known critical
  vulnerabilities in the package/security sources available at the time of the change.
- Keep validation behavior consistent across backend and frontend whenever both layers
  validate the same user input.

## Database fields policy
prefer explicit fields over fields stored in jsonb fields

## Local validation policy

- After each completed onboarding change, run both browser QA channels with
  `bash scripts/run-browser-qa-all.sh`; a single channel is not sufficient.
- Install the repository pre-push hook with `bash scripts/install-git-hooks.sh`.
  Every push must pass both suites. Do not bypass the hook to push failing tests.
- Run relevant unit/frontend tests during editing as well. Report checks that
  could not run; do not claim that local success confirms GitHub CI success.

## Pull request branch policy

- Before committing or opening a pull request while a feature branch is checked out,
  ask whether to continue using the current branch or create a new branch.
- If a pull request already exists for the current branch, create a separate branch for
  any new work unless the user explicitly asks to update the existing pull request.
- If the user explicitly instructs which branch strategy to use, follow that instruction.
- Keep unrelated or untracked local files out of commits unless the user explicitly asks
  to include them.
