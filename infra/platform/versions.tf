terraform {
  required_version = ">= 1.12.0, < 1.13.0"
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags { tags = local.tags }
}
