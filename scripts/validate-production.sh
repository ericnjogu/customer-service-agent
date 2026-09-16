#!/usr/bin/env bash
set -euo pipefail

namespace=customer-service-production

test "$(kubectl get nodes --no-headers | wc -l | tr -d ' ')" -eq 3
test "$(kubectl get nodes --no-headers | grep -c ' Ready')" -eq 3
kubectl -n "$namespace" wait --for=condition=Ready cluster/production-postgres --timeout=10m
kubectl -n "$namespace" rollout status deployment/one-css-customer-service-app --timeout=10m
kubectl -n "$namespace" rollout status deployment/one-css-customer-service-web --timeout=10m
kubectl -n "$namespace" get pods -o wide

curl --fail --show-error --silent --retry 6 --retry-all-errors https://css.ristoh.co.ke/ >/dev/null
curl --fail --show-error --silent --retry 6 --retry-all-errors https://css.ristoh.co.ke/api/healthz

kubectl -n argocd get applications \
  -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
