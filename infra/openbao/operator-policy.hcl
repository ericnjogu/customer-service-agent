# Kubernetes administrators may obtain a short-lived operational token.
# This is not root: no policy/auth changes, raw storage, or credential reads.
path "sys/health" { capabilities = ["read"] }
path "sys/storage/raft/configuration" { capabilities = ["read"] }
path "sys/storage/raft/snapshot" { capabilities = ["read"] }
path "sys/mounts" { capabilities = ["read"] }
path "sys/audit" { capabilities = ["read"] }
