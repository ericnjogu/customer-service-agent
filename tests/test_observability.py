import logging

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
