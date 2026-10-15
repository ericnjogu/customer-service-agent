import json
from pathlib import Path

from app.business_information import DEFINITION, clean_business_summary, has_business_content


def test_frontend_backend_definitions_match():
    assert DEFINITION == json.loads(Path("web/src/business-information.json").read_text())


def test_guidance_alone_is_not_business_content():
    template = "\n\n".join(
        f"## {title}\n\n[Guidance: {help_text}]" for title, help_text in DEFINITION["sections"]
    )
    assert not has_business_content(template)
    assert "Guidance:" not in clean_business_summary(template)
    assert has_business_content(template + "\nWe sell fresh bread.")
    assert "We sell fresh bread." in clean_business_summary(template + "\nWe sell fresh bread.")


def test_user_text_is_preserved():
    text = "## Products & services\n\n[Guidance: Our own policy.]\nBread."
    assert clean_business_summary(text) == text
