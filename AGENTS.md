# Project guidance for Codex agents

## Dependency policy

- Prefer established libraries over hand-rolled parsing/validation for standardized
  domains such as phone numbers, email addresses, URLs, dates/times, currencies, auth,
  crypto, and security-sensitive protocol handling.
- Before adding a dependency, check that it has recent releases and no known critical
  vulnerabilities in the package/security sources available at the time of the change.
- For phone-number parsing and validation, prefer libphonenumber-backed libraries:
  `phonenumbers` in Python and `libphonenumber-js` in the React/Vite app.
- Keep validation behavior consistent across backend and frontend whenever both layers
  validate the same user input.

## Pull request branch policy

- Before committing or opening a pull request while a feature branch is checked out,
  ask whether to continue using the current branch or create a new branch.
- If a pull request already exists for the current branch, create a separate branch for
  any new work unless the user explicitly asks to update the existing pull request.
- If the user explicitly instructs which branch strategy to use, follow that instruction.
- Keep unrelated or untracked local files out of commits unless the user explicitly asks
  to include them.
