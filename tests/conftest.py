"""Most existing tests exercise non-PostgreSQL application paths explicitly."""

import pytest


@pytest.fixture(autouse=True)
def disable_issue_worker_unless_requested(monkeypatch):
    monkeypatch.setenv("AGENT_ISSUE_PROCESSING_ENABLED", "false")
