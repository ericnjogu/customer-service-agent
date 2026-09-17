variable "cloudflare_api_token" {
  description = "Cloudflare token with DNS edit permission for the selected zone."
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone containing ristoh.co.ke."
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
