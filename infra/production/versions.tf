terraform {
  required_version = "~> 1.12.0"
  backend "local" {}
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.52.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.6.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

provider "github" {
  owner = var.github_owner
  token = var.github_token
}
