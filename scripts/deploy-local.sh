#!/usr/bin/env bash
set -euo pipefail

print_exit_timestamp() {
  local status=$?
  echo "deploy-local.sh finished at $(date '+%Y-%m-%d %H:%M:%S %Z') with exit status ${status}"
}

trap print_exit_timestamp EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RELEASE_NAME="${RELEASE_NAME:-cs-local}"
NAMESPACE="${NAMESPACE:-customer-service}"
CHART_PATH="${CHART_PATH:-${REPO_ROOT}/helm/customer-service}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-customer-service}"
IMAGE_TAG="${IMAGE_TAG:-local}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-${REPO_ROOT}/Dockerfile}"
WEB_IMAGE_REPOSITORY="${WEB_IMAGE_REPOSITORY:-customer-service-web}"
WEB_IMAGE_TAG="${WEB_IMAGE_TAG:-local}"
WEB_DOCKERFILE_PATH="${WEB_DOCKERFILE_PATH:-${REPO_ROOT}/web/Dockerfile}"
WEB_API_BASE_URL="${WEB_API_BASE_URL:-/api}"
WEB_PUBLIC_BASE_URL="${WEB_PUBLIC_BASE_URL:-http://localhost:5173}"
AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH="${AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH:-true}"
LOG_LEVEL="${LOG_LEVEL:-DEBUG}"
KUBE_CONTEXT="${KUBE_CONTEXT:-rancher-desktop}"
APP_DEPLOYMENT="${APP_DEPLOYMENT:-${RELEASE_NAME}-customer-service-app}"
WEB_DEPLOYMENT="${WEB_DEPLOYMENT:-${RELEASE_NAME}-customer-service-web}"
N8N_ENABLED="${N8N_ENABLED:-false}"
N8N_DEPLOYMENT="${N8N_DEPLOYMENT:-${RELEASE_NAME}-customer-service-n8n}"
N8N_OWNER_MANAGED_BY_ENV="${N8N_OWNER_MANAGED_BY_ENV:-false}"
N8N_OWNER_EMAIL="${N8N_OWNER_EMAIL:-}"
N8N_OWNER_FIRST_NAME="${N8N_OWNER_FIRST_NAME:-}"
N8N_OWNER_LAST_NAME="${N8N_OWNER_LAST_NAME:-}"
N8N_OWNER_SECRET_NAME="${N8N_OWNER_SECRET_NAME:-n8n-owner}"
N8N_ENCRYPTION_KEY_SECRET_NAME="${N8N_ENCRYPTION_KEY_SECRET_NAME:-${N8N_OWNER_SECRET_NAME}}"
N8N_EMAIL_RESEND_API_KEY_SECRET_NAME="${N8N_EMAIL_RESEND_API_KEY_SECRET_NAME:-api-keys}"
N8N_EMAIL_RESEND_API_KEY_SECRET_KEY="${N8N_EMAIL_RESEND_API_KEY_SECRET_KEY:-RESEND_API_KEY}"
N8N_ONBOARDING_EMAIL="${N8N_ONBOARDING_EMAIL:-}"
AGENT_EMAIL_PROVIDER="${AGENT_EMAIL_PROVIDER:-log}"
AGENT_EMAIL_FROM="${AGENT_EMAIL_FROM:-}"
AGENT_ONBOARDING_REVIEW_EMAIL="${AGENT_ONBOARDING_REVIEW_EMAIL:-${N8N_ONBOARDING_EMAIL}}"
AGENT_EMAIL_RESEND_API_KEY_SECRET_NAME="${AGENT_EMAIL_RESEND_API_KEY_SECRET_NAME:-${N8N_EMAIL_RESEND_API_KEY_SECRET_NAME}}"
AGENT_EMAIL_RESEND_API_KEY_SECRET_KEY="${AGENT_EMAIL_RESEND_API_KEY_SECRET_KEY:-${N8N_EMAIL_RESEND_API_KEY_SECRET_KEY}}"
AGENT_PLATFORM_WEB_SEARCH_PROVIDER="${AGENT_PLATFORM_WEB_SEARCH_PROVIDER:-tavily}"
AGENT_PLATFORM_WEB_SEARCH_PROJECT_ID="${AGENT_PLATFORM_WEB_SEARCH_PROJECT_ID:-ristoh-css}"
AGENT_PLATFORM_WEB_SEARCH_MAX_RESULTS="${AGENT_PLATFORM_WEB_SEARCH_MAX_RESULTS:-3}"
AGENT_PLATFORM_WEB_SEARCH_TIMEOUT_SECONDS="${AGENT_PLATFORM_WEB_SEARCH_TIMEOUT_SECONDS:-15}"
AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME="${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME:-api-keys}"
AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_KEY="${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_KEY:-PLATFORM_TAVILY_API_KEY}"
AGENT_PROVIDER_PROJECT_PROVISIONER="${AGENT_PROVIDER_PROJECT_PROVISIONER:-api}"
AGENT_OPENAI_ADMIN_KEY_SECRET_NAME="${AGENT_OPENAI_ADMIN_KEY_SECRET_NAME:-api-keys}"
AGENT_OPENAI_ADMIN_KEY_SECRET_KEY="${AGENT_OPENAI_ADMIN_KEY_SECRET_KEY:-OPENAI_ADMIN_KEY}"
AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL="${AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL:-}"

if [[ "${AGENT_EMAIL_PROVIDER}" == "resend" && -z "${AGENT_EMAIL_FROM}" ]]; then
  echo "AGENT_EMAIL_FROM is required when AGENT_EMAIL_PROVIDER=resend." >&2
  echo "It is passed to Helm as email.from and rendered in the app Pod as AGENT_EMAIL_FROM." >&2
  exit 1
fi

if [[ "${AGENT_PLATFORM_WEB_SEARCH_PROVIDER}" == "tavily" && -z "${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME}" ]]; then
  echo "AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME is required when AGENT_PLATFORM_WEB_SEARCH_PROVIDER=tavily." >&2
  echo "Create a Kubernetes Secret containing ${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_KEY}, then pass the Secret name." >&2
  exit 1
fi

if [[ "${AGENT_PROVIDER_PROJECT_PROVISIONER}" == "api" && -z "${AGENT_OPENAI_ADMIN_KEY_SECRET_NAME}" ]]; then
  echo "AGENT_OPENAI_ADMIN_KEY_SECRET_NAME is required when AGENT_PROVIDER_PROJECT_PROVISIONER=api." >&2
  echo "Create a Kubernetes Secret containing ${AGENT_OPENAI_ADMIN_KEY_SECRET_KEY}, then pass the Secret name." >&2
  exit 1
fi

echo "Using Kubernetes context: ${KUBE_CONTEXT}"
kubectl config use-context "${KUBE_CONTEXT}"

echo "Building image: ${IMAGE_REPOSITORY}:${IMAGE_TAG}"
nerdctl --namespace k8s.io build \
  -f "${DOCKERFILE_PATH}" \
  -t "${IMAGE_REPOSITORY}:${IMAGE_TAG}" \
  "${REPO_ROOT}"

echo "Building web image: ${WEB_IMAGE_REPOSITORY}:${WEB_IMAGE_TAG}"
nerdctl --namespace k8s.io build \
  -f "${WEB_DOCKERFILE_PATH}" \
  --build-arg "VITE_API_BASE_URL=${WEB_API_BASE_URL}" \
  --build-arg "VITE_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH=${AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH}" \
  -t "${WEB_IMAGE_REPOSITORY}:${WEB_IMAGE_TAG}" \
  "${REPO_ROOT}/web"

echo "Ensuring namespace exists: ${NAMESPACE}"
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

if [[ "${N8N_ENABLED}" == "true" && "${N8N_OWNER_MANAGED_BY_ENV}" == "true" ]]; then
  if [[ -z "${N8N_OWNER_EMAIL}" || -z "${N8N_OWNER_FIRST_NAME}" || -z "${N8N_OWNER_LAST_NAME}" ]]; then
    echo "N8N_OWNER_EMAIL, N8N_OWNER_FIRST_NAME, and N8N_OWNER_LAST_NAME are required when N8N_OWNER_MANAGED_BY_ENV=true." >&2
    exit 1
  fi

  echo "Checking n8n owner Secret exists: ${N8N_OWNER_SECRET_NAME}"
  kubectl get secret "${N8N_OWNER_SECRET_NAME}" --namespace "${NAMESPACE}" >/dev/null
fi

echo "Upgrading Helm release: ${RELEASE_NAME}"
helm_args=(
  upgrade --install "${RELEASE_NAME}" "${CHART_PATH}"
  --namespace "${NAMESPACE}"
  --set "image.repository=${IMAGE_REPOSITORY}"
  --set "image.tag=${IMAGE_TAG}"
  --set "web.image.repository=${WEB_IMAGE_REPOSITORY}"
  --set "web.image.tag=${WEB_IMAGE_TAG}"
  --set "web.apiBaseUrl=${WEB_API_BASE_URL}"
  --set "web.publicBaseUrl=${WEB_PUBLIC_BASE_URL}"
  --set "telegram.webhookPublicBaseUrl=${AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL}"
  --set "email.provider=${AGENT_EMAIL_PROVIDER}"
  --set "onboarding.requireAdminEmailDomainMatch=${AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH}"
  --set "platform.webSearchProvider=${AGENT_PLATFORM_WEB_SEARCH_PROVIDER}"
  --set "platform.webSearchProjectId=${AGENT_PLATFORM_WEB_SEARCH_PROJECT_ID}"
  --set "platform.webSearchMaxResults=${AGENT_PLATFORM_WEB_SEARCH_MAX_RESULTS}"
  --set "platform.webSearchTimeoutSeconds=${AGENT_PLATFORM_WEB_SEARCH_TIMEOUT_SECONDS}"
  --set "providerProjects.provisioner=${AGENT_PROVIDER_PROJECT_PROVISIONER}"
  --set "n8n.enabled=${N8N_ENABLED}"
  --set "logging.level=${LOG_LEVEL}"
)

if [[ -n "${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME}" ]]; then
  helm_args+=(
    --set "platform.webSearchApiKeySecretName=${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME}"
    --set "platform.webSearchApiKeySecretKey=${AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_KEY}"
  )
fi

if [[ -n "${AGENT_OPENAI_ADMIN_KEY_SECRET_NAME}" ]]; then
  helm_args+=(
    --set "providerProjects.openaiAdminKeySecretName=${AGENT_OPENAI_ADMIN_KEY_SECRET_NAME}"
    --set "providerProjects.openaiAdminKeySecretKey=${AGENT_OPENAI_ADMIN_KEY_SECRET_KEY}"
  )
fi

if [[ -n "${AGENT_EMAIL_FROM}" ]]; then
  helm_args+=(
    --set "email.from=${AGENT_EMAIL_FROM}"
  )
fi

if [[ -n "${AGENT_ONBOARDING_REVIEW_EMAIL}" ]]; then
  helm_args+=(
    --set "email.onboardingReviewEmail=${AGENT_ONBOARDING_REVIEW_EMAIL}"
  )
fi

if [[ -n "${AGENT_EMAIL_RESEND_API_KEY_SECRET_NAME}" ]]; then
  helm_args+=(
    --set "email.resendApiKeySecretName=${AGENT_EMAIL_RESEND_API_KEY_SECRET_NAME}"
    --set "email.resendApiKeySecretKey=${AGENT_EMAIL_RESEND_API_KEY_SECRET_KEY}"
  )
fi

if [[ "${N8N_ENABLED}" == "true" && "${N8N_OWNER_MANAGED_BY_ENV}" == "true" ]]; then
  helm_args+=(
    --set "n8n.owner.managedByEnv=true"
    --set "n8n.owner.email=${N8N_OWNER_EMAIL}"
    --set "n8n.owner.firstName=${N8N_OWNER_FIRST_NAME}"
    --set "n8n.owner.lastName=${N8N_OWNER_LAST_NAME}"
    --set "n8n.owner.existingSecret=${N8N_OWNER_SECRET_NAME}"
    --set "n8n.encryptionKey.existingSecret=${N8N_ENCRYPTION_KEY_SECRET_NAME}"
  )
fi

if [[ -n "${N8N_EMAIL_RESEND_API_KEY_SECRET_NAME}" ]]; then
  helm_args+=(
    --set "n8n.email.resendApiKeySecretName=${N8N_EMAIL_RESEND_API_KEY_SECRET_NAME}"
    --set "n8n.email.resendApiKeySecretKey=${N8N_EMAIL_RESEND_API_KEY_SECRET_KEY}"
  )
fi

if [[ -n "${N8N_ONBOARDING_EMAIL}" ]]; then
  helm_args+=(
    --set "n8n.email.onboardingEmail=${N8N_ONBOARDING_EMAIL}"
  )
fi

helm "${helm_args[@]}"

echo "Restarting deployment to pick up rebuilt local image: ${APP_DEPLOYMENT}"
kubectl rollout restart "deployment/${APP_DEPLOYMENT}" --namespace "${NAMESPACE}"
echo "Restarting web deployment to pick up rebuilt local image: ${WEB_DEPLOYMENT}"
kubectl rollout restart "deployment/${WEB_DEPLOYMENT}" --namespace "${NAMESPACE}"

echo "Waiting for rollout: ${APP_DEPLOYMENT}"
kubectl rollout status "deployment/${APP_DEPLOYMENT}" --namespace "${NAMESPACE}"
echo "Waiting for rollout: ${WEB_DEPLOYMENT}"
kubectl rollout status "deployment/${WEB_DEPLOYMENT}" --namespace "${NAMESPACE}"

echo "Deployment complete."
echo "View logs with:"
echo "  kubectl logs deployment/${APP_DEPLOYMENT} --namespace ${NAMESPACE}"
echo "  kubectl logs deployment/${WEB_DEPLOYMENT} --namespace ${NAMESPACE}"
