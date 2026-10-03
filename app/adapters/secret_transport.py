"""Prevent credential-bearing HTTP requests from leaking into telemetry."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry.instrumentation.utils import suppress_instrumentation

_sensitive_http = ContextVar("sensitive_http", default=False)


class _SecretHTTPFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # httpx INFO includes URLs; httpcore DEBUG can include response headers.
        return not _sensitive_http.get()


@contextmanager
def secret_transport():
    for name in (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _SecretHTTPFilter) for item in logger.filters):
            logger.addFilter(_SecretHTTPFilter())
    token = _sensitive_http.set(True)
    try:
        with suppress_instrumentation():
            yield
    finally:
        _sensitive_http.reset(token)
