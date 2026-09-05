variable "aws_region" {
  type    = string
  default = "eu-central-1"
  validation {
    condition     = var.aws_region == "eu-central-1"
    error_message = "This project permits Regional resources only in eu-central-1."
  }
}

variable "aws_account_id" {
  type    = string
  default = "371664303664"
}

variable "operator_cidr" {
  type        = string
  description = "Public CIDR allowed to reach the EKS API, such as 203.0.113.4/32. Keep this in an untracked tfvars file."
  nullable    = false

  validation {
    condition     = can(cidrnetmask(var.operator_cidr)) && var.operator_cidr != "0.0.0.0/0"
    error_message = "operator_cidr must be a valid restricted CIDR; 0.0.0.0/0 is forbidden."
  }
}

variable "kubernetes_operator_principal_arn" {
  type        = string
  description = "ARN of the IAM user allowed to assume the Kubernetes operator role."
  default     = "arn:aws:iam::371664303664:user/tmp-admin"

  validation {
    condition     = startswith(var.kubernetes_operator_principal_arn, "arn:aws:iam::${var.aws_account_id}:user/")
    error_message = "kubernetes_operator_principal_arn must be an IAM user in this AWS project."
  }
}

variable "kubernetes_operator_user_name" {
  type        = string
  description = "Name of the IAM user allowed to assume the Kubernetes operator role."
  default     = "tmp-admin"
}
