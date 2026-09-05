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
