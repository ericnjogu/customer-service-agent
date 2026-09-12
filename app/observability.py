import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.config import Settings

_log_record_factory_installed = False


def configure_tracing(app: FastAPI, settings: Settings) -> TracerProvider | None:
    if not settings.telemetry_enabled:
        return None

    provider = TracerProvider(
        resource=Resource.create(
            {
                SERVICE_NAME: settings.otel_service_name,
                DEPLOYMENT_ENVIRONMENT: settings.deployment_environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_trace_sample_ratio)),
    )
    endpoint = f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=".*/healthz")
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    AsyncPGInstrumentor().instrument(tracer_provider=provider)
    RedisInstrumentor().instrument(tracer_provider=provider)
    return provider


def set_tenant_trace_attributes(tenant_id: str | None, tenant_slug: str | None = None) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    if tenant_id:
        span.set_attribute("app.tenant.id", tenant_id)
        span.set_attribute("tenant_id", tenant_id)
    if tenant_slug:
        span.set_attribute("app.tenant.slug", tenant_slug)
        span.set_attribute("tenant_slug", tenant_slug)
    searchable = [
        key
        for key, value in (
            ("tenant_id", tenant_id),
            ("tenant_slug", tenant_slug),
        )
        if value
    ]
    if searchable:
        span.set_attribute("aws.xray.annotations", searchable)


def install_log_trace_context() -> None:
    global _log_record_factory_installed
    if _log_record_factory_installed:
        return

    previous_factory = logging.getLogRecordFactory()

    def record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        context = trace.get_current_span().get_span_context()
        record.trace_id = format(context.trace_id, "032x") if context.is_valid else "-"
        record.span_id = format(context.span_id, "016x") if context.is_valid else "-"
        return record

    logging.setLogRecordFactory(record_factory)
    _log_record_factory_installed = True


def shutdown_tracing(provider: TracerProvider | None) -> None:
    if provider is not None:
        provider.shutdown()
