locals {
  name = "ristoh-ai-chatbot"
  azs  = ["eu-central-1a", "eu-central-1b"]
  tags = {
    Project   = local.name
    ManagedBy = "OpenTofu"
  }
  public_subnets = {
    "eu-central-1a" = "10.60.0.0/24"
    "eu-central-1b" = "10.60.1.0/24"
  }
  workload_subnets = {
    "eu-central-1a" = "10.60.16.0/20"
    "eu-central-1b" = "10.60.32.0/20"
  }
  data_subnets = {
    "eu-central-1a" = "10.60.64.0/24"
    "eu-central-1b" = "10.60.65.0/24"
  }
  addon_versions = {
    vpc-cni        = "v1.22.4-eksbuild.3"
    kube-proxy     = "v1.36.0-eksbuild.17"
    coredns        = "v1.14.3-eksbuild.14"
    metrics-server = "v0.9.0-eksbuild.8"
  }
}

data "aws_caller_identity" "current" {}

check "expected_account" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
    error_message = "Refusing to manage the wrong AWS project."
  }
}

resource "aws_vpc" "this" {
  cidr_block           = "10.60.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  for_each                = local.public_subnets
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = each.value
  map_public_ip_on_launch = true
  tags = {
    Name                     = "${local.name}-public-${each.key}"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "workload" {
  for_each          = local.workload_subnets
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value
  tags = {
    Name                              = "${local.name}-workload-${each.key}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_subnet" "data" {
  for_each          = local.data_subnets
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value
  tags              = { Name = "${local.name}-data-${each.key}" }
}

resource "aws_eip" "nat" {
  for_each = local.public_subnets
  domain   = "vpc"
  tags     = { Name = "${local.name}-nat-${each.key}" }
}

resource "aws_nat_gateway" "this" {
  for_each      = local.public_subnets
  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = aws_subnet.public[each.key].id
  depends_on    = [aws_internet_gateway.this]
  tags          = { Name = "${local.name}-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "workload" {
  for_each = local.workload_subnets
  vpc_id   = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[each.key].id
  }
  tags = { Name = "${local.name}-workload-${each.key}" }
}

resource "aws_route_table_association" "workload" {
  for_each       = aws_subnet.workload
  subnet_id      = each.value.id
  route_table_id = aws_route_table.workload[each.key].id
}

resource "aws_route_table" "data" {
  for_each = local.data_subnets
  vpc_id   = aws_vpc.this.id
  tags     = { Name = "${local.name}-data-${each.key}" }
}

resource "aws_route_table_association" "data" {
  for_each       = aws_subnet.data
  subnet_id      = each.value.id
  route_table_id = aws_route_table.data[each.key].id
}

data "aws_iam_policy_document" "eks_cluster_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_cluster" {
  name               = "${local.name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_cluster_trust.json
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "eks_vpc_resource_controller" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
}

resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${local.name}/cluster"
  retention_in_days = 30
}

resource "aws_eks_cluster" "this" {
  name     = local.name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = "1.36"

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  vpc_config {
    subnet_ids              = values(aws_subnet.workload)[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.operator_cidr]
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster,
    aws_iam_role_policy_attachment.eks_vpc_resource_controller,
    aws_cloudwatch_log_group.eks
  ]
}

data "aws_iam_policy_document" "kubernetes_operator_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [var.kubernetes_operator_principal_arn]
    }
  }
}

resource "aws_iam_role" "kubernetes_operator" {
  name                 = "${local.name}-kubernetes-operator"
  description          = "Assumed by project team members for least-privilege Kubernetes access"
  assume_role_policy   = data.aws_iam_policy_document.kubernetes_operator_trust.json
  max_session_duration = 14400
}

data "aws_iam_policy_document" "assume_kubernetes_operator" {
  statement {
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.kubernetes_operator.arn]
  }
}

resource "aws_iam_user_policy" "assume_kubernetes_operator" {
  name   = "AssumeKubernetesOperatorRole"
  user   = var.kubernetes_operator_user_name
  policy = data.aws_iam_policy_document.assume_kubernetes_operator.json
}

resource "aws_iam_user_policy_attachment" "operator_local_login" {
  user       = var.kubernetes_operator_user_name
  policy_arn = "arn:aws:iam::aws:policy/SignInLocalDevelopmentAccess"
}

resource "aws_eks_access_entry" "kubernetes_operator" {
  cluster_name      = aws_eks_cluster.this.name
  principal_arn     = aws_iam_role.kubernetes_operator.arn
  kubernetes_groups = ["${local.name}-operator"]
  type              = "STANDARD"
}

resource "aws_eks_access_entry" "github_staging_deployer" {
  cluster_name      = aws_eks_cluster.this.name
  principal_arn     = "arn:aws:iam::${var.aws_account_id}:role/${local.name}-github-staging-deploy"
  kubernetes_groups = ["${local.name}-staging-deployer"]
  type              = "STANDARD"
}

data "tls_certificate" "eks" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
}

data "aws_iam_policy_document" "load_balancer_controller_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_role" "load_balancer_controller" {
  name               = "${local.name}-aws-load-balancer-controller"
  description        = "IRSA role for the AWS Load Balancer Controller"
  assume_role_policy = data.aws_iam_policy_document.load_balancer_controller_trust.json
}

data "http" "load_balancer_controller_iam_policy" {
  url = "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v3.5.0/docs/install/iam_policy.json"
}

resource "aws_iam_policy" "load_balancer_controller" {
  name        = "${local.name}-aws-load-balancer-controller"
  description = "Official AWS Load Balancer Controller v3.5.0 permissions"
  policy      = data.http.load_balancer_controller_iam_policy.response_body
}

resource "aws_iam_role_policy_attachment" "load_balancer_controller" {
  role       = aws_iam_role.load_balancer_controller.name
  policy_arn = aws_iam_policy.load_balancer_controller.arn
}

resource "aws_acm_certificate" "staging" {
  domain_name       = var.staging_hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name        = var.staging_hostname
    Environment = "staging"
  }
}

data "aws_iam_policy_document" "fargate_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks-fargate-pods.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:eks:${var.aws_region}:${var.aws_account_id}:fargateprofile/${local.name}/*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.aws_account_id]
    }
  }
}

resource "aws_iam_role" "fargate" {
  name               = "${local.name}-fargate-pod-execution"
  assume_role_policy = data.aws_iam_policy_document.fargate_trust.json
}

resource "aws_iam_role_policy_attachment" "fargate" {
  role       = aws_iam_role.fargate.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSFargatePodExecutionRolePolicy"
}

resource "aws_cloudwatch_log_group" "fargate" {
  name              = "/aws/eks/${local.name}/fargate"
  retention_in_days = 30
}

resource "aws_security_group" "codebuild_runner" {
  name        = "${local.name}-codebuild-runner"
  description = "Outbound-only access for ephemeral staging deployment runners"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "HTTPS to GitHub and AWS service endpoints through NAT"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_security_group_ingress_rule" "cluster_api_from_codebuild" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.codebuild_runner.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Private EKS API access from ephemeral CodeBuild runners"
}

resource "aws_cloudwatch_log_group" "codebuild_runner" {
  name              = "/aws/codebuild/${local.name}-staging-deploy"
  retention_in_days = 30
}

resource "aws_codeconnections_connection" "github" {
  name          = "${local.name}-github"
  provider_type = "GitHub"
}

data "aws_iam_policy_document" "codebuild_runner_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.aws_account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/${local.name}-staging-deploy"]
    }
  }
}

resource "aws_iam_role" "codebuild_runner" {
  name               = "${local.name}-codebuild-staging-deploy"
  description        = "Service role for the ephemeral staging GitHub Actions runner"
  assume_role_policy = data.aws_iam_policy_document.codebuild_runner_trust.json
}

data "aws_iam_policy_document" "codebuild_runner" {
  statement {
    sid = "ManageBuildNetworkInterfaces"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DeleteNetworkInterface",
      "ec2:DescribeDhcpOptions",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSubnets",
      "ec2:DescribeVpcs"
    ]
    resources = ["*"]
  }
  statement {
    sid       = "AuthorizeBuildNetworkInterfaces"
    actions   = ["ec2:CreateNetworkInterfacePermission"]
    resources = ["arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:network-interface/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:AuthorizedService"
      values   = ["codebuild.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "ec2:Subnet"
      values   = [for subnet in aws_subnet.workload : subnet.arn]
    }
  }
  statement {
    sid = "WriteRunnerLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["${aws_cloudwatch_log_group.codebuild_runner.arn}:*"]
  }
  statement {
    sid = "GetRepositoryConnectionToken"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:GetConnectionToken"
    ]
    resources = [aws_codeconnections_connection.github.arn]
  }
  statement {
    sid       = "UseOnlyApplicationRepository"
    actions   = ["codeconnections:UseConnection"]
    resources = [aws_codeconnections_connection.github.arn]
    condition {
      test     = "StringEquals"
      variable = "codeconnections:FullRepositoryId"
      values   = ["ericnjogu/customer-service-agent"]
    }
  }
}

resource "aws_iam_role_policy" "codebuild_runner" {
  name   = "RunPrivateGitHubDeploymentJobs"
  role   = aws_iam_role.codebuild_runner.id
  policy = data.aws_iam_policy_document.codebuild_runner.json
}

resource "aws_codebuild_project" "staging_deploy" {
  name                   = "${local.name}-staging-deploy"
  description            = "Ephemeral GitHub Actions runner for staging Helm deployments"
  service_role           = aws_iam_role.codebuild_runner.arn
  build_timeout          = 30
  queued_timeout         = 30
  concurrent_build_limit = 1

  artifacts {
    type = "NO_ARTIFACTS"
  }

  source {
    type            = "GITHUB"
    location        = "https://github.com/ericnjogu/customer-service-agent.git"
    git_clone_depth = 1
    buildspec       = ""
    auth {
      type     = "CODECONNECTIONS"
      resource = aws_codeconnections_connection.github.arn
    }
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
  }

  vpc_config {
    vpc_id             = aws_vpc.this.id
    subnets            = values(aws_subnet.workload)[*].id
    security_group_ids = [aws_security_group.codebuild_runner.id]
  }

  logs_config {
    cloudwatch_logs {
      status      = "ENABLED"
      group_name  = aws_cloudwatch_log_group.codebuild_runner.name
      stream_name = "runner"
    }
    s3_logs {
      status = "DISABLED"
    }
  }
}

resource "aws_codebuild_webhook" "staging_deploy" {
  project_name = aws_codebuild_project.staging_deploy.name
  build_type   = "BUILD"

  filter_group {
    filter {
      type    = "EVENT"
      pattern = "WORKFLOW_JOB_QUEUED"
    }
    filter {
      type    = "WORKFLOW_NAME"
      pattern = "^Build and deliver staging$"
    }
  }
}

data "aws_iam_policy_document" "fargate_logging" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents"
    ]
    resources = ["${aws_cloudwatch_log_group.fargate.arn}:*"]
  }
}

resource "aws_iam_role_policy" "fargate_logging" {
  name   = "FargateCloudWatchLogs"
  role   = aws_iam_role.fargate.id
  policy = data.aws_iam_policy_document.fargate_logging.json
}

resource "aws_eks_fargate_profile" "this" {
  for_each = toset(concat(
    ["kube-system", "external-secrets", "customer-service-staging"],
    var.retain_argocd_during_migration ? ["argocd"] : []
  ))

  cluster_name           = aws_eks_cluster.this.name
  fargate_profile_name   = replace(each.key, "customer-service-", "")
  pod_execution_role_arn = aws_iam_role.fargate.arn
  subnet_ids             = values(aws_subnet.workload)[*].id
  selector { namespace = each.key }
  depends_on = [aws_iam_role_policy_attachment.fargate, aws_iam_role_policy.fargate_logging]
}

data "aws_iam_policy_document" "vpc_cni_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:aws-node"]
    }
  }
}

resource "aws_iam_role" "vpc_cni" {
  name               = "${local.name}-vpc-cni"
  assume_role_policy = data.aws_iam_policy_document.vpc_cni_trust.json
}

resource "aws_iam_role_policy_attachment" "vpc_cni" {
  role       = aws_iam_role.vpc_cni.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_eks_addon" "this" {
  for_each = local.addon_versions

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  service_account_role_arn    = each.key == "vpc-cni" ? aws_iam_role.vpc_cni.arn : null
  configuration_values = (
    each.key == "coredns" ? jsonencode({
      computeType  = "Fargate"
      replicaCount = 1
      }) : each.key == "metrics-server" ? jsonencode({
      replicas = 1
    }) : null
  )

  depends_on = [aws_eks_fargate_profile.this, aws_iam_role_policy_attachment.vpc_cni]
}

resource "aws_ecr_repository" "application" {
  for_each = toset(["customer-service", "customer-service-web"])

  name                 = each.key
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = false }
  encryption_configuration { encryption_type = "AES256" }
  tags = {
    Environment = "staging"
    Workload    = "customer-support"
  }

  lifecycle { prevent_destroy = true }
}

import {
  to = aws_ecr_repository.application["customer-service"]
  id = "customer-service"
}

import {
  to = aws_ecr_repository.application["customer-service-web"]
  id = "customer-service-web"
}
