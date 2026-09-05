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
