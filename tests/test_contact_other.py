import pytest
from pydantic import ValidationError

from app.models import OnboardingContactPoint


@pytest.mark.parametrize("url", [None, "mailto:hello@example.com", "tel:+254700000000"])
def test_other_requires_web_url(url):
    with pytest.raises(ValidationError, match="'Other' requires"):
        OnboardingContactPoint(kind="other", value="@example", url=url)


def test_other_accepts_web_url():
    point = OnboardingContactPoint(kind="other", label="Other", url="https://example.com")
    assert point.url == "https://example.com"
