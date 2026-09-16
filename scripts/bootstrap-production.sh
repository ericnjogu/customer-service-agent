#!/usr/bin/env bash
set -euo pipefail

: "${SOPS_AGE_KEY_FILE:?Set SOPS_AGE_KEY_FILE to the production age private-key file}"

ARGOCD_CHART_VERSION="10.9.0"
CNPG_CHART_VERSION="0.29.0"
REPOSITORY="https://github.com/ericnjogu/customer-service-agent.git"
REVISION="deploy/production"
HELM_BIN="${HELM_BIN:-helm}"

helm_version="$(${HELM_BIN} version --template '{{.Version}}')"
case "${helm_version}" in
  v3.*) ;;
  *)
    echo "Helm 3 is required; found ${helm_version}. Set HELM_BIN to a Helm 3 binary." >&2
    exit 1
    ;;
esac

if ! git ls-remote --exit-code --heads "${REPOSITORY}" "${REVISION}" >/dev/null; then
  echo "${REVISION} does not exist yet. Merge the image workflow and let it publish real production digests first." >&2
  exit 1
fi

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace sops --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sops create secret generic sops-age-key-file \
  --from-file="key=${SOPS_AGE_KEY_FILE}" \
  --dry-run=client -o yaml | kubectl apply -f -

"${HELM_BIN}" repo add argo https://argoproj.github.io/argo-helm --force-update
"${HELM_BIN}" repo add cnpg https://cloudnative-pg.github.io/charts --force-update
"${HELM_BIN}" template cloudnative-pg cnpg/cloudnative-pg \
  --namespace cnpg-system \
  --version "${CNPG_CHART_VERSION}" \
  --show-only templates/crds/crds.yaml | kubectl apply \
  --server-side \
  --field-manager=production-bootstrap \
  -f -

"${HELM_BIN}" upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --version "${ARGOCD_CHART_VERSION}" \
  --values gitops/argocd-values.yaml \
  --atomic --wait --timeout 15m

# Install the CRD providers before the root Application evaluates resources that
# use ClusterIssuer and SopsSecret. Application-of-Applications sync waves cannot
# bypass API discovery for kinds that do not exist yet.
kubectl apply \
  -f gitops/production/cert-manager.yaml \
  -f gitops/production/sops-secrets-operator.yaml
kubectl -n argocd wait \
  --for=jsonpath='{.status.sync.status}'=Synced \
  application/cert-manager application/sops-secrets-operator \
  --timeout=10m
kubectl -n argocd wait \
  --for=jsonpath='{.status.health.status}'=Healthy \
  application/cert-manager application/sops-secrets-operator \
  --timeout=10m

sed -e "s|https://github.com/ericnjogu/customer-service-agent.git|${REPOSITORY}|" \
  -e "s|deploy/production|${REVISION}|" \
  gitops/production/root-application.yaml | kubectl apply -f -

kubectl -n argocd wait --for=condition=Available deployment/argocd-server --timeout=5m
kubectl -n argocd get applications
