# OpenTofu infrastructure

OpenTofu 1.12.x owns the replacement platform. State is split into four roots and all
Regional resources are fixed to `eu-central-1`:

1. `bootstrap`: S3 state storage and repository-scoped GitHub OIDC roles.
2. `platform`: VPC, EKS/Fargate, add-ons, logging, and the two imported ECR repositories.
3. `staging`: fresh PostgreSQL, Valkey, security groups, and workload IRSA.
4. `cluster-bootstrap`: private Argo CD and the staging root Application.

Create an untracked `backend.hcl` in each root from its example. Create
`infra/platform/operator.auto.tfvars` containing the operator's current restricted public
CIDR. Never commit that file.

Bootstrap the state bucket once with local state, then migrate that root. On a brand-new
project, temporarily comment out the `backend "s3" {}` block in
`infra/bootstrap/versions.tf` for the initial `init` and `apply`; restore it before the
migration command. (`-backend=false` alone does not override a declared backend for an
apply.)

```bash
AWS_PROFILE=new-project tofu -chdir=infra/bootstrap init
AWS_PROFILE=new-project tofu -chdir=infra/bootstrap apply
cp infra/bootstrap/backend.hcl.example infra/bootstrap/backend.hcl
AWS_PROFILE=new-project tofu -chdir=infra/bootstrap init -migrate-state -backend-config=backend.hcl
```

For each remaining root, copy the backend example, initialize, save and inspect a plan,
then apply locally in order. OpenTofu applies are intentionally absent from GitHub Actions.

```bash
AWS_PROFILE=new-project tofu -chdir=infra/platform init -backend-config=backend.hcl
AWS_PROFILE=new-project tofu -chdir=infra/platform plan -out=platform.tfplan
AWS_PROFILE=new-project tofu -chdir=infra/platform apply platform.tfplan
```

Repeat for `staging` and `cluster-bootstrap`. Before cluster bootstrap, update
`values-staging.yaml` from the staging outputs. The checked-in staging profile deliberately
uses local/extractive providers and therefore does not require an API-key Secret. If a
remote provider is enabled later, create its Secret in `customer-service-staging` without
checking secret values into Git.

The ECR `import` blocks intentionally transfer only `customer-service` and
`customer-service-web`. OpenTofu never adopts or destroys the old eksctl/CloudFormation
platform.
