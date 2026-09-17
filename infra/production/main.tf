locals {
  origin_names = ["production-1", "production-2", "production-3"]
}

resource "cloudflare_load_balancer_monitor" "https" {
  account_id     = var.cloudflare_account_id
  type           = "https"
  port           = 443
  method         = "GET"
  path           = "/api/healthz"
  expected_codes = "200"
  interval       = 60
  timeout        = 5
  retries        = 2
  description    = "Production web health"
  header {
    header = "Host"
    values = [var.hostname]
  }
}

resource "cloudflare_load_balancer_pool" "production" {
  account_id         = var.cloudflare_account_id
  name               = "ristoh-production"
  monitor            = cloudflare_load_balancer_monitor.https.id
  minimum_origins    = 1
  notification_email = var.notification_email
  dynamic "origins" {
    for_each = { for index, address in var.origin_ipv4_addresses : local.origin_names[index] => address }
    content {
      name    = origins.key
      address = origins.value
      enabled = true
      weight  = 1
    }
  }
}

resource "cloudflare_load_balancer" "production" {
  zone_id          = var.cloudflare_zone_id
  name             = var.hostname
  default_pool_ids = [cloudflare_load_balancer_pool.production.id]
  fallback_pool_id = cloudflare_load_balancer_pool.production.id
  proxied          = true
  steering_policy  = "dynamic_latency"
  session_affinity = "none"
  description      = "Ristoh customer service production"
}
