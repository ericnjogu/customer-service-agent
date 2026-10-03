from app.config import Settings


def test_whatsapp_signup_enabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_ONBOARDING_WHATSAPP_ENABLED", raising=False)
    assert Settings(_env_file=None).onboarding_whatsapp_enabled is True


def test_whatsapp_signup_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_ONBOARDING_WHATSAPP_ENABLED", "false")
    assert Settings(_env_file=None).onboarding_whatsapp_enabled is False
