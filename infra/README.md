# OpenTofu infrastructure

OpenTofu 1.12.x owns the replacement platform. State is split into four roots and all
Regional resources are fixed to `eu-central-1`:

1. `bootstrap`: S3 state storage and repository-scoped GitHub OIDC roles.
2. `platform`: VPC, EKS/Fargate, add-ons, logging, and the two imported ECR repositories.
3. `staging`: fresh PostgreSQL, Valkey, security groups, and workload IRSA.
4. `cluster-bootstrap`: External Secrets Operator, AWS Load Balancer Controller, and
   shared Kubernetes resources that are not owned by the application release.

The GitHub OIDC trust uses the immutable owner and repository IDs emitted in this
repository's token subject. If the repository is transferred or recreated, update
`github_oidc_repository` from the subject observed in CloudTrail before applying the
bootstrap root.

The cluster-bootstrap root installs External Secrets Operator 2.8.0. Its controller
uses IRSA to read only `ristoh-ai-chatbot/staging/api-keys` and
`ristoh-ai-chatbot/staging/app-configs` from Secrets Manager. It maintains the `api-keys`
and `app-configs` Kubernetes Secrets in `customer-service-staging`. The latter contains
the email settings and `AGENT_WEBHOOK_PUBLIC_BASE_URL`; secret values remain
outside Git and OpenTofu state.

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
and port-forward pods in `customer-service-staging`. It cannot read Secrets, exec into
pods, administer RBAC or namespaces, or create/delete workloads.

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

## Staging delivery

Merges to `main` build immutable `linux/amd64` images on GitHub-hosted runners and push
the full commit SHA to the two ECR repositories. The deployment job runs on the ephemeral
CodeBuild runner `ristoh-ai-chatbot-staging-deploy` in the private workload subnets. It
assumes `ristoh-ai-chatbot-github-staging-deploy`, connects to the private EKS endpoint,
and runs Helm with the two resolved image digests. When no Helm release exists yet, the
first run reads and adopts the exact digests already running in staging; subsequent runs
deploy the newly built digests. CodeBuild terminates the runner after the single job;
there is no continuously running delivery controller.

The OpenTofu-created CodeConnections resource remains `PENDING` until a project
administrator completes its GitHub App authorization once:

1. Apply `bootstrap` to create the deployment role. Initialize `platform`, then create
   only the pending connection and copy its ARN:

   ```bash
   AWS_PROFILE=root-infra-bootstrap tofu -chdir=infra/platform apply \
     -target=aws_codeconnections_connection.github
   ```
2. In AWS Console, select `eu-central-1`, then open Developer Tools → Settings →
   Connections. Select the pending connection and choose **Update pending connection**
   to authorize the AWS Connector for GitHub.
3. In GitHub, verify the AWS Connector for GitHub is installed—not merely listed under
   authorized applications—and grant it access only to
   `ericnjogu/customer-service-agent`. If the connection is already `AVAILABLE` but
   CodeBuild cannot create its webhook, install it directly from
   <https://github.com/apps/aws-connector-for-github/installations/new>.
4. Confirm the connection status is `AVAILABLE`, then run a reviewed `platform` plan and
   apply with `-var=retain_argocd_during_migration=true`. This creates the runner and
   webhook without deleting the active Argo Fargate profile.

The deployment role is mapped only to the `ristoh-ai-chatbot-staging-deployer` Kubernetes
group. Helm uses the ConfigMap storage driver so CI has no permission to read Kubernetes
Secrets. Application RBAC for tenant Telegram Secret access is owned by the locally
applied cluster-foundation chart instead of the CI-managed application chart.

Manual rollback uses the administrator context and the same Helm driver:

```bash
HELM_DRIVER=configmap helm --kube-context ristoh-ai-chatbot-admin rollback aws-csa \
  --namespace customer-service-staging
```

## Argo CD retirement

Do not remove Argo until the CodeConnections status is `AVAILABLE`, the CodeBuild webhook
exists, and one deployment job has successfully adopted the current `aws-csa` resources.
The application deployment uses `--take-ownership`, `--atomic`, and `--wait` so the first
successful run establishes Helm ownership without changing the deployment mechanism
mid-rollout.

After that validation:

1. Remove the finalizer from `argocd/aws-csa-staging`, then delete that Application. This
   preserves the staging resources.
2. Apply `cluster-bootstrap`. Its `removed` block forgets the old `gitops-root` release
   without uninstalling shared objects, the new `cluster-foundation` release adopts those
   objects, and the Argo Helm release is uninstalled.
3. Delete the empty `argocd` namespace and remaining `argoproj.io` CRDs with the admin
   context.
4. Apply `platform` normally, without the migration variable, to delete only the
   `argocd` Fargate profile.
5. Verify staging pods, External Secrets, the load balancer controller, public web URL,
   and `/api/healthz` before considering the migration complete.

If the CodeBuild deployment fails, leave Argo installed and correct the replacement path
before performing any retirement step. OpenTofu applies remain local; GitHub Actions can
deploy the application but cannot apply infrastructure.

## Public staging endpoint

The platform root requests an ACM certificate for `staging.css.ristoh.co.ke` and outputs
the DNS validation CNAME. Because the `ristoh.co.ke` authoritative DNS service is external
to AWS, create that record with the DNS provider and wait for the certificate to become
`ISSUED`. The cluster-bootstrap root installs AWS Load Balancer Controller 3.5.0, and the
staging Helm release creates an internet-facing HTTPS ALB for the web service. After the
Ingress reports an ALB hostname, create this external DNS record:

```text
staging.css.ristoh.co.ke CNAME <ingress ALB hostname>
```

The browser URL is `https://staging.css.ristoh.co.ke`; nginx proxies `/api/*` to the
private API service. Set `AGENT_WEBHOOK_PUBLIC_BASE_URL` in the
`ristoh-ai-chatbot/staging/app-configs` AWS secret to
`https://staging.css.ristoh.co.ke/api` so Telegram receives the public API route.
