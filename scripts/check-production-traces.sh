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
    2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
spans = {
    span.get("name"): span
    for batch in data.get("batches", data.get("resourceSpans", []))
    for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", []))
    for span in scope.get("spans", [])
}
web, api = spans.get("web.api", {}), spans.get("GET /onboarding/config", {})
sys.exit(0 if web.get("spanId") and api.get("parentSpanId") == web["spanId"] else 1)
'; then
    echo "Connected web/API trace stored successfully: ${trace_id}"
    echo "In Grafana Explore, select Tempo and look up this trace ID."
    exit 0
  fi
  sleep 2
done

echo "Connected web/API spans for ${trace_id} were not found. Check web/application telemetry, collector export errors, Tempo readiness, and tunnel access." >&2
exit 1
