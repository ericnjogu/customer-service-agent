variable "cloudflare_api_token" {
  description = "Cloudflare token with DNS and Load Balancing edit permissions for the selected zone."
  type        = string
  sensitive   = true
}

variable "github_token" {
  description = "Fine-grained GitHub token allowed to manage Actions variables for the repository."
  type        = string
  sensitive   = true
}

variable "github_owner" {
  type    = string
  default = "ericnjogu"
}

variable "github_repository" {
  type    = string
  default = "customer-service-agent"
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone containing ristoh.co.ke."
  type        = string
}

variable "cloudflare_account_id" {
  description = "Cloudflare account that owns the production zone and load balancer."
  type        = string
}

variable "origin_ipv4_addresses" {
  description = "Public IPv4 addresses for the three one.com production servers."
  type        = list(string)
  validation {
    condition     = length(var.origin_ipv4_addresses) == 3 && length(distinct(var.origin_ipv4_addresses)) == 3
    error_message = "Exactly three unique production origin addresses are required."
  }
}

variable "hostname" {
  description = "Production hostname."
  type        = string
  default     = "css.ristoh.co.ke"
}

variable "notification_email" {
  description = "Address that receives Cloudflare load-balancer health notifications."
  type        = string
}
