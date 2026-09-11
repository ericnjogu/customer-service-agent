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

resource "helm_release" "aws_for_fluent_bit" {
  name       = "aws-for-fluent-bit"
  namespace  = "kube-system"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-for-fluent-bit"
  version    = "0.2.0"
  wait       = true
  timeout    = 1200

  values = [yamlencode({
    serviceAccount = {
      create = true
      name   = "aws-for-fluent-bit"
      annotations = {
        "eks.amazonaws.com/role-arn" = data.terraform_remote_state.platform.outputs.fluent_bit_role_arn
      }
    }
    cloudWatchLogs = {
      enabled           = true
      region            = var.aws_region
      logGroupName      = data.terraform_remote_state.platform.outputs.container_log_group_name
      logStreamPrefix   = "ec2-"
      autoCreateGroup   = false
      autoRetryRequests = true
    }
    resources = {
      requests = { cpu = "50m", memory = "50Mi" }
      limits   = { memory = "250Mi" }
    }
    priorityClassName = "system-node-critical"
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
