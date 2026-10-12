import pytest
from pydantic import ValidationError

from app.adapters.meta_signup import VerifiedWhatsAppAsset
from app.config import Settings
from app.whatsapp_signup import WhatsAppSignupError, WhatsAppSignupService


def test_mode_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("AGENT_ONBOARDING_WHATSAPP_MODE", raising=False)
    assert Settings(_env_file=None).onboarding_whatsapp_mode == "cloud_api"
    assert WhatsAppSignupService(None, None, None).mode == "cloud_api"
    settings = Settings(_env_file=None, onboarding_whatsapp_mode="coexistence")
    assert settings.onboarding_whatsapp_mode == "coexistence"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, onboarding_whatsapp_mode="anything")


@pytest.mark.parametrize("mode,on_app,platform,error", [
    ("coexistence", True, "CLOUD_API", None),
    ("coexistence", False, "CLOUD_API", "coexistence_business_app_number_required"),
    ("cloud_api", False, "CLOUD_API", None),
    ("cloud_api", False, "NOT_APPLICABLE", "cloud_api_registration_required"),
    ("cloud_api", True, "CLOUD_API", "use_coexistence_for_business_app_number"),
])
def test_asset_mode(mode, on_app, platform, error):
    service = WhatsAppSignupService(None, None, None, mode=mode)
    asset = VerifiedWhatsAppAsset("123", "456", "+254700000000", on_app, platform)
    if error:
        with pytest.raises(WhatsAppSignupError, match=error):
            service.validate_asset_mode(asset)
    else:
        service.validate_asset_mode(asset)
