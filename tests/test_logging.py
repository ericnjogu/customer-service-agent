import logging

from app.main import HealthzAccessLogFilter, configure_logging


def make_access_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def make_access_record_with_args(message: str, args: tuple) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def test_healthz_access_log_filter_suppresses_health_checks() -> None:
    log_filter = HealthzAccessLogFilter()

    assert log_filter.filter(
        make_access_record('10.42.0.1:42356 - "GET /healthz HTTP/1.1" 200 OK')
    ) is False


def test_healthz_access_log_filter_allows_other_requests() -> None:
    log_filter = HealthzAccessLogFilter()

    assert log_filter.filter(
        make_access_record('10.42.0.1:42356 - "POST /webhooks/synthetic HTTP/1.1" 200 OK')
    ) is True


def test_healthz_access_log_filter_does_not_format_awkward_percent_messages() -> None:
    log_filter = HealthzAccessLogFilter()

    assert (
        log_filter.filter(make_access_record_with_args("raw percent % message", ("/healthz",)))
        is False
    )


def test_configure_logging_uses_valid_formatter() -> None:
    configure_logging("DEBUG", "{asctime} - {levelname}:{name}:{message}")
    formatter = logging.getLogger().handlers[0].formatter
    record = logging.LogRecord(
        name="app.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Starting %s with configured_log_level=%s effective_log_level=%s",
        args=("customer-support-agent", "DEBUG", "DEBUG"),
        exc_info=None,
    )

    assert "customer-support-agent" in formatter.format(record)


def test_configure_logging_uses_curly_brace_style() -> None:
    configure_logging("INFO", "{levelname}|{name}|{message}")
    formatter = logging.getLogger().handlers[0].formatter
    record = logging.LogRecord(
        name="app.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    assert formatter.format(record) == "INFO|app.main|hello world"
