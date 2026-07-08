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
    formatter = logging.Formatter("{asctime} - {levelname}:{name}:{message}", style="{")
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
    formatter = logging.Formatter("{levelname}|{name}|{message}", style="{")
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


def test_configure_logging_does_not_force_existing_logger_levels() -> None:
    app_logger = logging.getLogger("app")
    original_level = app_logger.level
    try:
        app_logger.setLevel(logging.WARNING)

        configure_logging("DEBUG", "{levelname}:{message}")
        assert app_logger.level == logging.WARNING
    finally:
        app_logger.setLevel(original_level)
