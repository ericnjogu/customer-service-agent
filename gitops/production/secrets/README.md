# Production secrets

Commit only SOPS-encrypted `SopsSecret` resources in this directory. The production
age private key is never stored in Git. Bootstrap it as follows after installing the
operator namespace:

```bash
kubectl create namespace sops --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sops create secret generic sops-age-key-file \
  --from-file=key="$SOPS_AGE_KEY_FILE"
```

Copy `production-secrets.example.yaml` to an untracked temporary path, populate it,
and encrypt the `secretTemplates` structure:

```bash
sops encrypt --age "$SOPS_AGE_RECIPIENTS" \
  --encrypted-suffix Templates --mac-only-encrypted \
  /private/tmp/production-secrets.yaml \
  > gitops/production/secrets/production-secrets.enc.yaml
```

The encrypted output is safe to commit. Confirm it contains no plaintext values.

The `backup-credentials` template uses fil.one S3-compatible credentials. The same
key may be used during bootstrap, but production should use a rotated key scoped to
the private `ristoh-css-postgres` bucket.
