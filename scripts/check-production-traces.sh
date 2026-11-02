#!/usr/bin/env bash
# Read-only application request plus a trace lookup; requires the production tunnel.
set -euo pipefail

base_url="${WEB_PUBLIC_BASE_URL:-https://css.ristoh.co.ke}"
trace_id="$(openssl rand -hex 16)"
parent_id="$(openssl rand -hex 8)"

# healthz is intentionally excluded from tracing. This public config GET has no
# signup side effects. ParentBased sampling honors this sampled W3C parent.
curl --fail --silent --show-error --max-time 20 \
  -H "traceparent: 00-${trace_id}-${parent_id}-01" \
  "${base_url%/}/api/onboarding/config" >/dev/null

for attempt in $(seq 1 30); do
  if kubectl --request-timeout=5s get --raw \
    "/api/v1/namespaces/monitoring/services/http:tempo:3200/proxy/api/traces/${trace_id}" \
    >/dev/null 2>&1; then
    echo "Trace stored successfully: ${trace_id}"
    echo "In Grafana Explore, select Tempo and look up this trace ID."
    exit 0
  fi
  sleep 2
done

echo "Trace ${trace_id} was not found. Check application telemetry, collector export errors, Tempo readiness, and tunnel access." >&2
exit 1
