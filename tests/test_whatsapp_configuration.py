import pytest
from pydantic import ValidationError

from app.config import Settings
from app.whatsapp_signup import WhatsAppSignupService


def test_whatsapp_attempt_timeout_configuration(monkeypatch):
    key = "AGENT_ONBOARDING_WHATSAPP_ATTEMPT_TTL_MINUTES"
    monkeypatch.delenv(key, raising=False)
    assert Settings(_env_file=None).onboarding_whatsapp_attempt_ttl_minutes == 30
    monkeypatch.setenv(key, "60")
    assert Settings(_env_file=None).onboarding_whatsapp_attempt_ttl_minutes == 60
    for value in (0, 121):
        monkeypatch.setenv(key, str(value))
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
        with pytest.raises(ValueError, match="timeout"):
            WhatsAppSignupService(None, None, None, attempt_ttl_minutes=value)


def test_whatsapp_signup_enabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_ONBOARDING_WHATSAPP_ENABLED", raising=False)
    assert Settings(_env_file=None).onboarding_whatsapp_enabled is True


def test_whatsapp_signup_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_WHATSAPP_ENABLED", "false")
    assert Settings(_env_file=None).onboarding_whatsapp_enabled is False
