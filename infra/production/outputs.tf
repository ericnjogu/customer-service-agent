output "production_hostname" {
  value = var.hostname
}

output "production_record_ids" {
  value = { for name, record in cloudflare_record.production : name => record.id }
}
