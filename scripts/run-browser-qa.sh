#!/usr/bin/env bash
set -euo pipefail

# Rancher Desktop/containerd by default; Docker-compatible engines are optional.
runtime="${AGENT_E2E_CONTAINER_RUNTIME:-nerdctl}"
command -v "$runtime" >/dev/null || {
  echo "Start Rancher Desktop and install $runtime before running browser QA." >&2
  exit 1
}
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_id="css-browser-qa-$$-${RANDOM}"
containers=()
test_pid=""

cleanup() {
  result=$?
  trap - EXIT
  # npm/the terminal can send another signal while cleanup is in progress.
  trap '' INT TERM HUP
  if [[ -n "$test_pid" ]]; then
    kill -TERM "$test_pid" 2>/dev/null || true
    wait "$test_pid" 2>/dev/null || true
  fi
  for container in "${containers[@]}"; do
    if ! "$runtime" rm -f -v "$container" >/dev/null; then
      echo "Could not remove test container: $container" >&2
      [[ "$result" -ne 0 ]] || result=1
    fi
  done
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

start_container() {
  local name="$run_id-$1"
  shift
  # Register before starting so a partially failed startup is cleaned up too.
  containers+=("$name")
  "$runtime" run -d --name "$name" "$@" >/dev/null
}

wait_ready() {
  local description="$1"
  shift
  for attempt in {1..90}; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for $description" >&2
  return 1
}

echo "Starting disposable PostgreSQL, Redis and WireMock containers with $runtime..."
start_container postgres -p 127.0.0.1::5432 \
  -e POSTGRES_DB=customer_service_e2e -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
start_container redis -p 127.0.0.1::6379 redis:7.4.5-alpine
start_container wiremock -p 127.0.0.1::8080 wiremock/wiremock:3.13.2

mapped_port() {
  "$runtime" port "$run_id-$1" "$2/tcp" | awk -F: 'NR == 1 {print $NF}'
}
postgres_port="$(mapped_port postgres 5432)"
redis_port="$(mapped_port redis 6379)"
wiremock_port="$(mapped_port wiremock 8080)"
export AGENT_E2E_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:$postgres_port/customer_service_e2e"
export AGENT_E2E_REDIS_URL="redis://127.0.0.1:$redis_port/0"
export AGENT_WIREMOCK_URL="http://127.0.0.1:$wiremock_port"

wait_ready PostgreSQL "$runtime" exec "$run_id-postgres" \
  pg_isready -U postgres -d customer_service_e2e
wait_ready Redis "$runtime" exec "$run_id-redis" redis-cli ping
wait_ready WireMock curl --fail --silent --max-time 2 \
  "$AGENT_WIREMOCK_URL/__admin/mappings"

cd "$repo_dir/web"
node node_modules/@playwright/test/cli.js test "$@" &
test_pid=$!
result=0
wait "$test_pid" || result=$?
test_pid=""
exit "$result"
