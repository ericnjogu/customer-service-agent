"""Most existing tests exercise non-PostgreSQL application paths explicitly."""

import pytest


@pytest.fixture(autouse=True)
def disable_issue_worker_unless_requested(monkeypatch):
    monkeypatch.setenv("AGENT_ISSUE_PROCESSING_ENABLED", "false")


@pytest.fixture(autouse=True)
def disable_whatsapp_signup_unless_requested(monkeypatch):
    # Isolated tests do not provision a vault or Meta application.
    monkeypatch.setenv("AGENT_ONBOARDING_WHATSAPP_ENABLED", "false")
