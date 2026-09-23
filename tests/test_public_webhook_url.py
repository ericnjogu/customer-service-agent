import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "web_url,environment,expected",
    [
        ("https://css.ristoh.co.ke", "production", "https://css.ristoh.co.ke/api"),
        ("https://demo.ngrok-free.app/", "local", "https://demo.ngrok-free.app/api"),
        ("http://localhost:8080", "local", None),
        ("http://127.0.0.1:18081", "test", "http://127.0.0.1:18081/api"),
    ],
)
def test_webhook_api_url_uses_public_web_url(web_url, environment, expected):
    settings = Settings(
        _env_file=None,
        web_public_base_url=web_url,
        deployment_environment=environment,
        onboarding_verification_code_secret="test-verification-secret-at-least-32-chars",
    )
    assert settings.public_webhook_api_url == expected
