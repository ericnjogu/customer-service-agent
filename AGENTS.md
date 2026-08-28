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
