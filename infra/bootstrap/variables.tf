variable "aws_region" {
  type        = string
  description = "AWS selected Region."
  default     = "eu-central-1"

  validation {
    condition     = var.aws_region == "eu-central-1"
    error_message = "This project permits Regional resources only in eu-central-1."
  }
}

variable "aws_account_id" {
  type        = string
  description = "AWS project account ID."
  default     = "371664303664"
}

variable "github_repository" {
  type        = string
  description = "GitHub owner/repository allowed to assume the CI roles."
  default     = "ericnjogu/customer-service-agent"
}
