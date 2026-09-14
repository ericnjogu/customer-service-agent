#!/usr/bin/env bash
set -euo pipefail

: "${SOPS_AGE_KEY_FILE:?Set SOPS_AGE_KEY_FILE to the production age private-key file}"

ARGOCD_CHART_VERSION="10.9.0"
REPOSITORY="https://github.com/ericnjogu/customer-service-agent.git"
REVISION="deploy/production"

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace sops --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sops create secret generic sops-age-key-file \
  --from-file="key=${SOPS_AGE_KEY_FILE}" \
  --dry-run=client -o yaml | kubectl apply -f -

helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --version "${ARGOCD_CHART_VERSION}" \
  --values gitops/argocd-values.yaml \
  --atomic --wait --timeout 15m

sed -e "s|https://github.com/ericnjogu/customer-service-agent.git|${REPOSITORY}|" \
  -e "s|deploy/production|${REVISION}|" \
  gitops/production/root-application.yaml | kubectl apply -f -

kubectl -n argocd wait --for=condition=Available deployment/argocd-server --timeout=5m
kubectl -n argocd get applications
