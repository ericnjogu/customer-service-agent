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

resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  namespace        = "external-secrets"
  create_namespace = true
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = "2.8.0"
  wait             = true
  timeout          = 1200

  values = [yamlencode({
    installCRDs = true
    serviceAccount = {
      create = true
      name   = "external-secrets"
      annotations = {
        "eks.amazonaws.com/role-arn" = data.terraform_remote_state.staging.outputs.external_secrets_role_arn
      }
    }
    resources = {
      requests = { cpu = "100m", memory = "128Mi" }
      limits   = { memory = "256Mi" }
    }
    webhook = {
      create = false
    }
    certController = {
      create = false
    }
  })]
}

resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  namespace  = "kube-system"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "3.5.0"
  wait       = true
  timeout    = 1200

  values = [yamlencode({
    clusterName  = data.terraform_remote_state.platform.outputs.cluster_name
    region       = var.aws_region
    vpcId        = data.terraform_remote_state.platform.outputs.vpc_id
    replicaCount = 1
    serviceAccount = {
      create = true
      name   = "aws-load-balancer-controller"
      annotations = {
        "eks.amazonaws.com/role-arn" = data.terraform_remote_state.platform.outputs.load_balancer_controller_role_arn
      }
    }
    resources = {
      requests = { cpu = "100m", memory = "256Mi" }
      limits   = { memory = "512Mi" }
    }
  })]
}

resource "helm_release" "observability_collector" {
  name             = "observability-collector"
  namespace        = "amazon-cloudwatch"
  create_namespace = true
  chart            = "${path.module}/../../helm/observability-collector"
  wait             = true
  timeout          = 600

  values = [yamlencode({
    serviceAccount = {
      roleArn = data.terraform_remote_state.platform.outputs.otel_collector_role_arn
    }
    clusterName        = data.terraform_remote_state.platform.outputs.cluster_name
    environment        = "staging"
    region             = var.aws_region
    collectionInterval = "60s"
    configRevision     = filesha256("${path.module}/../../helm/observability-collector/templates/resources.yaml")
    exporters = {
      aws = {
        enabled           = true
        containerLogGroup = data.terraform_remote_state.platform.outputs.container_log_group_name
        metricsLogGroup   = data.terraform_remote_state.platform.outputs.container_metrics_log_group_name
        metricsNamespace  = "RistohAiChatbot/EKS"
      }
    }
  })]
}

removed {
  from = helm_release.gitops_root

  lifecycle {
    destroy = false
  }
}

resource "helm_release" "cluster_foundation" {
  name           = "cluster-foundation"
  namespace      = "kube-system"
  chart          = "${path.module}/../../helm/cluster-foundation"
  take_ownership = true
  wait           = true
  timeout        = 600
  depends_on     = [helm_release.external_secrets, helm_release.aws_load_balancer_controller]

  values = [yamlencode({
    workloadSecurityGroupId = data.terraform_remote_state.staging.outputs.workload_security_group_id
    region                  = var.aws_region
  })]
}
