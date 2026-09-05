data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket       = "ristoh-ai-chatbot-tofu-state-${var.aws_account_id}-${var.aws_region}"
    key          = "platform/terraform.tfstate"
    region       = var.aws_region
    encrypt      = true
    use_lockfile = true
  }
}

data "terraform_remote_state" "staging" {
  backend = "s3"
  config = {
    bucket       = "ristoh-ai-chatbot-tofu-state-${var.aws_account_id}-${var.aws_region}"
    key          = "staging/terraform.tfstate"
    region       = var.aws_region
    encrypt      = true
    use_lockfile = true
  }
}

resource "helm_release" "argocd" {
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "oci://ghcr.io/argoproj/argo-helm"
  chart            = "argo-cd"
  version          = "10.8.0"
  wait             = true
  timeout          = 1200

  values = [yamlencode({
    global = {
      domain = "argocd.internal"
    }
    configs = {
      cm = {
        "admin.enabled"                      = "false"
        "application.resourceTrackingMethod" = "annotation"
      }
      params = {
        "server.insecure" = true
      }
    }
    server = {
      service = { type = "ClusterIP" }
      resources = {
        requests = { cpu = "100m", memory = "128Mi" }
        limits   = { memory = "256Mi" }
      }
    }
    controller = {
      resources = {
        requests = { cpu = "250m", memory = "512Mi" }
        limits   = { memory = "1Gi" }
      }
    }
    repoServer = {
      resources = {
        requests = { cpu = "100m", memory = "256Mi" }
        limits   = { memory = "512Mi" }
      }
    }
    redis = {
      resources = {
        requests = { cpu = "100m", memory = "128Mi" }
        limits   = { memory = "256Mi" }
      }
    }
    dex            = { enabled = false }
    notifications  = { enabled = false }
    applicationSet = { enabled = false }
  })]
}

resource "helm_release" "gitops_root" {
  name       = "gitops-root"
  namespace  = "argocd"
  chart      = "${path.module}/../../gitops/root-chart"
  wait       = true
  timeout    = 600
  depends_on = [helm_release.argocd]

  values = [yamlencode({
    repository              = "https://github.com/ericnjogu/customer-service-agent.git"
    revision                = "deploy/staging"
    workloadSecurityGroupId = data.terraform_remote_state.staging.outputs.workload_security_group_id
    fargateLogGroup         = "/aws/eks/ristoh-ai-chatbot/fargate"
    region                  = var.aws_region
  })]
}
