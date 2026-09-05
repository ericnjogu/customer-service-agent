locals {
  name       = "ristoh-ai-chatbot"
  env_name   = "staging"
  cache_name = "${local.name}-${local.env_name}-cache"
  tags = {
    Project     = local.name
    Environment = local.env_name
    ManagedBy   = "OpenTofu"
  }
}

data "aws_caller_identity" "current" {}

check "expected_account" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
    error_message = "Refusing to manage the wrong AWS project."
  }
}

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

resource "aws_security_group" "workload" {
  name        = "${local.name}-${local.env_name}-workload"
  description = "Security group assigned only to staging application pods"
  vpc_id      = data.terraform_remote_state.platform.outputs.vpc_id

  egress {
    description = "Application egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "database" {
  name        = "${local.name}-${local.env_name}-database"
  description = "PostgreSQL from staging application pods only"
  vpc_id      = data.terraform_remote_state.platform.outputs.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.workload.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from staging workload"
}

resource "aws_security_group" "cache" {
  name        = "${local.name}-${local.env_name}-cache"
  description = "Valkey from staging application pods only"
  vpc_id      = data.terraform_remote_state.platform.outputs.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "cache" {
  security_group_id            = aws_security_group.cache.id
  referenced_security_group_id = aws_security_group.workload.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Valkey TLS from staging workload"
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-${local.env_name}"
  subnet_ids = data.terraform_remote_state.platform.outputs.data_subnet_ids
}

data "aws_iam_policy_document" "database_kms" {
  statement {
    sid       = "EnableAccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.aws_account_id}:root"]
    }
  }
  statement {
    sid = "AllowRDSLogEncryption"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey"
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/rds/instance/${local.name}-${local.env_name}/*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.aws_account_id]
    }
  }
}

resource "aws_kms_key" "database" {
  description             = "RDS and RDS log encryption for ${local.name} staging"
  deletion_window_in_days = 14
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.database_kms.json
}

resource "aws_kms_alias" "database" {
  name          = "alias/${local.name}-${local.env_name}-rds"
  target_key_id = aws_kms_key.database.key_id
}

resource "aws_cloudwatch_log_group" "postgresql" {
  name              = "/aws/rds/instance/${local.name}-${local.env_name}/postgresql"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.database.arn
}

resource "aws_db_parameter_group" "postgresql" {
  name   = "${local.name}-${local.env_name}-postgres18"
  family = "postgres18"
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "postgresql" {
  identifier = "${local.name}-${local.env_name}"

  engine                        = "postgres"
  engine_version                = "18.6"
  instance_class                = "db.t4g.micro"
  allocated_storage             = 20
  max_allocated_storage         = 100
  storage_type                  = "gp3"
  storage_encrypted             = true
  kms_key_id                    = aws_kms_key.database.arn
  multi_az                      = true
  publicly_accessible           = false
  db_name                       = "risto_css"
  username                      = "risto_app_owner"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.database.key_id
  port                          = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.postgresql.name

  backup_retention_period         = 7
  deletion_protection             = true
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.database.arn
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.name}-${local.env_name}-final"

  tags = {
    created_by       = "rds-oss-skill"
    generation_model = "gpt-5"
  }

  depends_on = [aws_cloudwatch_log_group.postgresql]
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.name}-${local.env_name}"
  subnet_ids = data.terraform_remote_state.platform.outputs.data_subnet_ids
}

resource "random_password" "disabled_cache_default" {
  length  = 64
  special = true
}

resource "aws_elasticache_user" "default" {
  user_id       = "${local.name}-${local.env_name}-default"
  user_name     = "default"
  engine        = "valkey"
  access_string = "off ~* -@all"
  authentication_mode {
    type      = "password"
    passwords = [random_password.disabled_cache_default.result]
  }
}

resource "aws_elasticache_user" "application" {
  user_id       = "${local.name}-${local.env_name}-app"
  user_name     = "${local.name}-${local.env_name}-app"
  engine        = "valkey"
  access_string = "on ~* +@all"
  authentication_mode { type = "iam" }
}

resource "aws_elasticache_user_group" "this" {
  engine        = "valkey"
  user_group_id = "${local.name}-${local.env_name}"
  user_ids      = [aws_elasticache_user.default.user_id, aws_elasticache_user.application.user_id]
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.cache_name
  description          = "Staging tenant configuration cache"
  engine               = "valkey"
  engine_version       = "9.0"
  node_type            = "cache.t4g.micro"
  port                 = 6379
  num_cache_clusters   = 2

  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  apply_immediately          = true

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.cache.id]
  user_group_ids     = [aws_elasticache_user_group.this.user_group_id]

  snapshot_retention_limit = 7
  snapshot_window          = "02:00-03:00"
  maintenance_window       = "sun:03:00-sun:04:00"

  tags = {
    managed_by       = "aws-skills"
    skill            = "elasticache"
    skill_version    = "2.0.0"
    created_by       = "elasticache-skill"
    generation_model = "gpt-5"
  }
}

data "aws_iam_policy_document" "workload_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.terraform_remote_state.platform.outputs.cluster_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(data.terraform_remote_state.platform.outputs.cluster_oidc_issuer, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(data.terraform_remote_state.platform.outputs.cluster_oidc_issuer, "https://", "")}:sub"
      values   = ["system:serviceaccount:customer-service-staging:aws-csa-customer-service-app"]
    }
  }
}

resource "aws_iam_role" "workload" {
  name               = "${local.name}-${local.env_name}-workload"
  assume_role_policy = data.aws_iam_policy_document.workload_trust.json
}

data "aws_iam_policy_document" "workload" {
  statement {
    sid       = "ReadManagedDatabaseCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.postgresql.master_user_secret[0].secret_arn]
  }
  statement {
    sid       = "DecryptManagedDatabaseCredentials"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.database.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "ConnectToStagingValkey"
    actions   = ["elasticache:Connect"]
    resources = [aws_elasticache_replication_group.this.arn, aws_elasticache_user.application.arn]
  }
}

resource "aws_iam_role_policy" "workload" {
  name   = "StagingDataAccess"
  role   = aws_iam_role.workload.id
  policy = data.aws_iam_policy_document.workload.json
}
