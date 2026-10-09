import pytest

from app.notifications import LoggingEmailSender
from app.verification_email import verification_email_html


def test_verification_email_prominent_code_and_escaped_fields():
    html = verification_email_html(
        name='<script>alert("x")</script>',
        code="001234",
        purpose="your account email address",
        resume_url='https://example.com/?session_id=abc&x="test"',
        ttl_minutes=10,
    )
    assert "001234</p>" in html
    assert "font-size:44px" in html
    assert "expires in 10 minutes" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;x=&quot;test&quot;" in html
    assert "does not verify your email" in html


@pytest.mark.asyncio
async def test_email_sender_keeps_html_and_plain_text():
    sender = LoggingEmailSender()
    await sender.send_email(
        to=["test@example.com"], subject="Verify", text="001234", html="<p>001234</p>"
    )
    assert sender.sent_messages[0].text == "001234"
    assert sender.sent_messages[0].html == "<p>001234</p>"
