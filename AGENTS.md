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

## AWS guidance for the new AWS experience

This project uses the AWS new experience for AWS work.

### Terminology

- Say "project" instead of "account" where possible; a project contains an AWS
  account and settings for sharing with other collaborators.
- Say "team member" instead of "IAM user" for human access; users are invited by
  email, not created or federated in IAM.
- Say "AWS Settings" for project management, billing, team members, and spend
  limits at <https://settings.aws.com/>. AWS resources are still viewed and
  managed in the AWS Management Console.
- Say "selected Region" instead of "home Region".

### Region and service constraints

- The selected Region for this project is `eu-central-1`.
- Create Regional resources in `eu-central-1` unless a global AWS service
  explicitly requires `us-east-1`.
- Do not create Lambda, API Gateway, or other Regional resources outside
  `eu-central-1`.
- Do not use Lambda@Edge.
- Do not use CloudFormation StackSets.
- Do not design for cross-Region replication or multi-Region KMS keys in this
  project.
- Do not use Route 53 geolocation, latency-based, or failover routing policies
  for this project.
- CloudFront is global and may require actions in `us-east-1`, but its Regional
  origins should remain in `eu-central-1`.
- If service availability or permissions look unexpectedly blocked, check the
  AWS project plan/spend status and whether the service is supported in the AWS
  new experience before assuming the application configuration is wrong.

### Access and billing guidance

- IAM permissions for human access are managed by AWS in the new experience.
  Do not assign roles to team members unless absolutely necessary.
- If resources suddenly become inaccessible or launch requests fail, ask whether
  the project has a spend limit or payment/billing restriction.
- Ask whether successfully-created AWS resources should be cleaned up or kept
  when a task is blocked, to reduce unnecessary cost.
- Keep secrets out of chat and tool output. Prefer AWS Secrets Manager and
  Kubernetes External Secrets for application secrets.

### Guidance level

- Preferred AWS guidance level: to be confirmed by the user.
- Low: only flag security risks.
- Medium: ask a couple of clarifying questions if something seems off.
- High: explain what is happening, suggest alternatives, and flag best practices.
