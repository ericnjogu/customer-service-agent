# OpenTofu infrastructure

OpenTofu 1.12.x owns the replacement platform. State is split into four roots and all
Regional resources are fixed to `eu-central-1`:

1. `bootstrap`: S3 state storage and repository-scoped GitHub OIDC roles.
2. `platform`: VPC, EKS/Fargate, add-ons, logging, and the two imported ECR repositories.
3. `staging`: fresh PostgreSQL, Valkey, security groups, and workload IRSA.
4. `cluster-bootstrap`: private Argo CD and the staging root Application.

The GitHub OIDC trust uses the immutable owner and repository IDs emitted in this
repository's token subject. If the repository is transferred or recreated, update
`github_oidc_repository` from the subject observed in CloudTrail before applying the
bootstrap root.

The cluster-bootstrap root also installs External Secrets Operator 2.8.0. Its controller
uses IRSA to read only `ristoh-ai-chatbot/staging/api-keys` from Secrets Manager and
maintains the `api-keys` Kubernetes Secret in `customer-service-staging`. Secret values
remain outside Git and OpenTofu state.

The platform root also creates `ristoh-ai-chatbot-kubernetes-operator`, a role with no
AWS service permissions. Its EKS access entry maps it to namespace-scoped Kubernetes
RBAC installed by the cluster-bootstrap root. The base IAM user can only refresh its
local AWS login and assume this role. Use the role for routine `kubectl` access; the
cluster creator's administrator access is reserved for local infrastructure bootstrap.

After both roots have been applied, create the operator context and make it current:

```bash
AWS_PROFILE=kubernetes-operator-base aws eks update-kubeconfig \
  --region eu-central-1 \
  --name ristoh-ai-chatbot \
  --alias ristoh-ai-chatbot-operator \
  --user-alias ristoh-ai-chatbot-operator \
  --role-arn arn:aws:iam::371664303664:role/ristoh-ai-chatbot-kubernetes-operator
kubectl config set-context ristoh-ai-chatbot-operator --namespace customer-service-staging
```

The operator can inspect pods, workloads, services, endpoints and events; read pod logs;
and port-forward pods in `argocd` and `customer-service-staging`. It can patch only the
`aws-csa-staging` Argo CD Application. It cannot read Secrets, exec into pods, administer
RBAC or namespaces, or create/delete workloads.

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
uses local/extractive providers, but its optional remote integrations reference the
External Secrets-managed `api-keys` Secret without checking values into Git.

The ECR `import` blocks intentionally transfer only `customer-service` and
`customer-service-web`. OpenTofu never adopts or destroys the old eksctl/CloudFormation
platform.
