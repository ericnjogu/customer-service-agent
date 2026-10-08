"""Shared, inline-styled verification email for email-client compatibility."""

# Inline email markup keeps complete style attributes together.
# ruff: noqa: E501

from html import escape


def verification_email_html(
    *, name: str, code: str, purpose: str, resume_url: str, ttl_minutes: int
) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;background:#f3f5f9;font-family:Arial,Helvetica,sans-serif;color:#344054;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"
 style="max-width:600px;background:#ffffff;">
<tr><td align="center" style="padding:32px 24px;background:#07152e;">
<span style="font-size:32px;font-weight:700;color:#ffffff;">Ristoh <span style="color:#22d3ce;">AI</span></span>
</td></tr>
<tr><td style="padding:32px 28px;">
<h1 style="margin:0 0 24px;font-size:28px;color:#172033;">Verify your email</h1>
<p style="font-size:16px;line-height:1.6;">Hello {escape(name)},</p>
<p style="font-size:16px;line-height:1.6;">Enter the following code on the onboarding page to verify {escape(purpose)}.</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">
<tr><td align="center" style="padding:28px 0 8px;">
<p style="margin:0 0 12px;font-size:16px;font-weight:700;">Verification code</p>
<p style="margin:0;font-size:44px;line-height:1.3;font-weight:700;letter-spacing:6px;color:#07152e;">{escape(code)}</p>
<p style="margin:16px 0 24px;font-size:14px;line-height:1.5;">This code expires in {ttl_minutes} minutes. Do not share it with anyone.</p>
</td></tr></table>
<p style="font-size:16px;line-height:1.6;">You can <a href="{escape(resume_url, quote=True)}" style="color:#2457eb;">resume onboarding</a> if you closed the page. Opening this link does not verify your email.</p>
<p style="font-size:14px;line-height:1.6;color:#667085;">If you did not request this code, you can safely ignore this email.</p>
</td></tr>
<tr><td align="center" style="padding:20px;background:#eef2f8;font-size:12px;color:#667085;">Ristoh AI · Customer-service onboarding</td></tr>
</table></td></tr></table></body></html>"""
