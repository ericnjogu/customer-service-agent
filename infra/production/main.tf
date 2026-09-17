locals {
  origin_names = ["production-1", "production-2", "production-3"]
  origins      = { for index, address in var.origin_ipv4_addresses : local.origin_names[index] => address }
}

resource "cloudflare_record" "production" {
  for_each = local.origins

  zone_id = var.cloudflare_zone_id
  name    = var.hostname
  type    = "A"
  content = each.value
  ttl     = 1
  proxied = true
  comment = "Ristoh customer service production ${each.key}"
}
