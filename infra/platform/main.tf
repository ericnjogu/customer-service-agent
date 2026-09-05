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

data "tls_certificate" "eks" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
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
  for_each = toset(["kube-system", "argocd", "external-secrets", "customer-service-staging"])

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
  configuration_values        = each.key == "coredns" ? jsonencode({ computeType = "Fargate" }) : null

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
