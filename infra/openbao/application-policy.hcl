# The application cannot administer OpenBao, list tenants, or delete credentials.
path "tenant-credentials/data/whatsapp/*" {
  capabilities = ["create", "update", "read"]
}
path "tenant-credentials/metadata/whatsapp/*" {
  capabilities = ["read"]
}
