locals {
  project_name = "ristoh-ai-chatbot"
  state_bucket = "${local.project_name}-tofu-state-${var.aws_account_id}-${var.aws_region}"
  tags = {
    Project   = local.project_name
    ManagedBy = "OpenTofu"
  }
}

data "aws_caller_identity" "current" {}

check "expected_account" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
    error_message = "Refusing to manage the wrong AWS project."
  }
}

data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.state.arn,
        "${aws_s3_bucket.state.arn}/*"
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = data.tls_certificate.github.url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

data "aws_iam_policy_document" "github_plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_oidc_repository}:pull_request",
        "repo:${var.github_oidc_repository}:ref:refs/heads/main"
      ]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name               = "${local.project_name}-github-plan"
  assume_role_policy = data.aws_iam_policy_document.github_plan_trust.json
}

resource "aws_iam_role_policy_attachment" "github_plan_readonly" {
  role       = aws_iam_role.github_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "github_state" {
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["bootstrap/*", "platform/*", "staging/*", "cluster-bootstrap/*"]
    }
  }
  statement {
    sid = "ReadStateAndManageLockfiles"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject"
    ]
    resources = [
      "${aws_s3_bucket.state.arn}/bootstrap/*",
      "${aws_s3_bucket.state.arn}/platform/*",
      "${aws_s3_bucket.state.arn}/staging/*",
      "${aws_s3_bucket.state.arn}/cluster-bootstrap/*"
    ]
  }
}

resource "aws_iam_role_policy" "github_state" {
  name   = "OpenTofuStateAccess"
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.github_state.json
}

data "aws_iam_policy_document" "github_ecr_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_oidc_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_ecr" {
  name               = "${local.project_name}-github-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.github_ecr_trust.json
}

data "aws_iam_policy_document" "github_ecr" {
  statement {
    sid       = "Authorization"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushApplicationImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage"
    ]
    resources = [
      "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/customer-service",
      "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/customer-service-web"
    ]
  }
}

resource "aws_iam_role_policy" "github_ecr" {
  name   = "PushApplicationImages"
  role   = aws_iam_role.github_ecr.id
  policy = data.aws_iam_policy_document.github_ecr.json
}
