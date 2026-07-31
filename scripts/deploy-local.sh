#!/usr/bin/env bash
set -euo pipefail

print_exit_timestamp() {
  local status=$?
  echo "deploy-local.sh finished at $(date '+%Y-%m-%d %H:%M:%S %Z') with exit status ${status}"
}

trap print_exit_timestamp EXIT

RELEASE_NAME="${RELEASE_NAME:-cs-local}"
NAMESPACE="${NAMESPACE:-customer-service}"
CHART_PATH="${CHART_PATH:-helm/customer-service}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-customer-service}"
IMAGE_TAG="${IMAGE_TAG:-local}"
LOG_LEVEL="${LOG_LEVEL:-DEBUG}"
KUBE_CONTEXT="${KUBE_CONTEXT:-rancher-desktop}"
APP_DEPLOYMENT="${APP_DEPLOYMENT:-${RELEASE_NAME}-customer-service-app}"

echo "Using Kubernetes context: ${KUBE_CONTEXT}"
kubectl config use-context "${KUBE_CONTEXT}"

echo "Building image: ${IMAGE_REPOSITORY}:${IMAGE_TAG}"
nerdctl --namespace k8s.io build -t "${IMAGE_REPOSITORY}:${IMAGE_TAG}" .

echo "Ensuring namespace exists: ${NAMESPACE}"
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "Upgrading Helm release: ${RELEASE_NAME}"
helm upgrade --install "${RELEASE_NAME}" "${CHART_PATH}" \
  --namespace "${NAMESPACE}" \
  --set "image.repository=${IMAGE_REPOSITORY}" \
  --set "image.tag=${IMAGE_TAG}" \
  --set "logging.level=${LOG_LEVEL}"

echo "Restarting deployment to pick up rebuilt local image: ${APP_DEPLOYMENT}"
kubectl rollout restart "deployment/${APP_DEPLOYMENT}" --namespace "${NAMESPACE}"

echo "Waiting for rollout: ${APP_DEPLOYMENT}"
kubectl rollout status "deployment/${APP_DEPLOYMENT}" --namespace "${NAMESPACE}"

echo "Deployment complete."
echo "View logs with:"
echo "  kubectl logs deployment/${APP_DEPLOYMENT} --namespace ${NAMESPACE}"
