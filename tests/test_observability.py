import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased

from app import observability


class RecordingSpan:
    def __init__(self, *, recording: bool = True) -> None:
        self.recording = recording
        self.attributes: dict[str, object] = {}

    def is_recording(self) -> bool:
        return self.recording

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_tenant_trace_attributes_are_searchable_in_xray(monkeypatch) -> None:
    span = RecordingSpan()
    monkeypatch.setattr(observability.trace, "get_current_span", lambda: span)

    observability.set_tenant_trace_attributes("tnt_123", "harbor-pine")

    assert span.attributes == {
        "app.tenant.id": "tnt_123",
        "app.tenant.slug": "harbor-pine",
        "tenant_id": "tnt_123",
        "tenant_slug": "harbor-pine",
        "aws.xray.annotations": ["tenant_id", "tenant_slug"],
    }


def test_tenant_trace_attributes_do_not_write_to_non_recording_span(monkeypatch) -> None:
    span = RecordingSpan(recording=False)
    monkeypatch.setattr(observability.trace, "get_current_span", lambda: span)

    observability.set_tenant_trace_attributes("tnt_123", "harbor-pine")

    assert span.attributes == {}


def test_log_record_factory_supplies_empty_trace_context() -> None:
    observability.install_log_trace_context()

    record = logging.getLogRecordFactory()(
        "app.test",
        logging.INFO,
        __file__,
        1,
        "message",
        (),
        None,
    )

    assert record.trace_id == "-"
    assert record.span_id == "-"


@pytest.mark.parametrize("db_key", ["db.system", "db.system.name"])
def test_polling_is_dropped_but_operation_database_children_remain(db_key):
    provider = TracerProvider(sampler=ParentBased(observability.OperationRootSampler(1)))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(
        "SELECT", kind=trace.SpanKind.CLIENT, attributes={db_key: "postgresql"}
    ) as span:
        assert not span.is_recording()
    with tracer.start_as_current_span("issue.process") as operation:
        with tracer.start_as_current_span(
            "SELECT", kind=trace.SpanKind.CLIENT, attributes={db_key: "postgresql"}
        ) as db:
            assert db.is_recording()
            assert db.get_span_context().trace_id == operation.get_span_context().trace_id
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["SELECT", "issue.process"]
    assert spans[0].parent.span_id == spans[1].context.span_id
    provider.shutdown()


@pytest.mark.parametrize("sampled", [False, True])
def test_remote_sampling_decision_is_respected(sampled):
    # A sampled parent overrides zero root sampling; an unsampled parent stays off.
    provider = TracerProvider(sampler=ParentBased(observability.OperationRootSampler(0)))
    ctx = trace.set_span_in_context(
        trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=123,
                span_id=456,
                is_remote=True,
                trace_flags=trace.TraceFlags(int(sampled)),
            )
        )
    )
    with provider.get_tracer("test").start_as_current_span(
        "request", context=ctx, kind=trace.SpanKind.SERVER
    ) as span:
        assert span.is_recording() == sampled
        with provider.get_tracer("test").start_as_current_span(
            "SELECT", kind=trace.SpanKind.CLIENT, attributes={"db.system": "postgresql"}
        ) as db:
            assert db.is_recording() == sampled
    provider.shutdown()


async def test_operation_records_safe_error_and_parent_without_arguments(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability.trace, "get_tracer", provider.get_tracer)

    @observability.traced_operation("conversation.answer")
    async def operation(secret):
        raise ValueError(secret)

    with provider.get_tracer("test").start_as_current_span("request"):
        with pytest.raises(ValueError):
            await operation("SYNTHETIC_SECRET")
    child, parent = exporter.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.status.status_code == trace.StatusCode.ERROR
    assert child.status.description is None
    assert child.attributes == {"error.type": "ValueError"}
    assert not child.events
    provider.shutdown()


async def test_graph_operations_are_children_of_a_single_trace(monkeypatch):
    from app.config import Settings
    from app.container import create_container
    from app.graph import invoke_service_graph
    from app.models import IncomingMessage

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability.trace, "get_tracer", provider.get_tracer)
    container = await create_container(Settings(issue_processing_enabled=False))
    try:
        with provider.get_tracer("test").start_as_current_span("request") as root:
            await invoke_service_graph(
                container.graph,
                IncomingMessage(
                    event_id="tracing-test",
                    external_chat_id="chat",
                    external_user_id="user",
                    text="How do I reset my password?",
                ),
            )
        spans = exporter.get_finished_spans()
        names = {s.name for s in spans}
        assert {
            "conversation.process",
            "conversation.plan",
            "conversation.retrieve",
            "conversation.answer",
            "conversation.persist_reply",
        } <= names
        assert {s.context.trace_id for s in spans} == {root.get_span_context().trace_id}
        assert "How do I reset" not in str([s.attributes for s in spans])
    finally:
        await container.close()
        provider.shutdown()
