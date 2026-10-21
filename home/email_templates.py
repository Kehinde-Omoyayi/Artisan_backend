"""Shared, fully branded multipart email delivery for security codes."""

from django.conf import settings
from .email_brand import BrandedEmailMultiAlternatives as EmailMultiAlternatives
from django.template.loader import render_to_string


def send_security_code_email(
    *,
    recipient_email,
    recipient_name,
    subject,
    heading,
    intro,
    code,
    code_label,
    security_note,
    purpose_label="Account security",
    action_url="",
    action_label="Continue securely",
):
    """Send an Artisan-styled HTML security email with a dependable text fallback."""
    name = " ".join(str(recipient_name or "").split()) or "there"
    code = str(code or "").strip()
    action_url = str(action_url or "").strip()
    context = {
        "brand_name": "Artisan Market",
        "recipient_name": name,
        "subject": subject,
        "heading": heading,
        "intro": intro,
        "code": code,
        "code_label": code_label,
        "security_note": security_note,
        "purpose_label": purpose_label,
        "action_url": action_url,
        "action_label": action_label,
        "preview_text": f"{heading} — your one-time security code is ready.",
    }
    text_lines = [
        "ARTISAN MARKET",
        "=" * 16,
        purpose_label.upper(),
        "",
        heading,
        "",
        f"Hello {name},",
        "",
        intro,
        "",
        f"{code_label}: {code}",
        "",
        "This one-time code expires in 5 minutes and can only be used once.",
        "",
        security_note,
    ]
    if action_url:
        text_lines.extend(("", f"{action_label}: {action_url}"))
    text_lines.extend(("", "Artisan Market Support", "A marketplace for artisan work in Nigeria."))
    text_body = "\n".join(text_lines)
    html_body = render_to_string("emails/security_code.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[recipient_email],
    )
    message.attach_alternative(html_body, "text/html")
    return message.send(fail_silently=False)
