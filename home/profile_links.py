"""Validation helpers for optional public professional links on artisan profiles."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


SOCIAL_MEDIA_PLATFORM_CHOICES = (
    ("INSTAGRAM", "Instagram"),
    ("FACEBOOK", "Facebook"),
    ("TIKTOK", "TikTok"),
    ("YOUTUBE", "YouTube"),
    ("LINKEDIN", "LinkedIn"),
    ("X", "X"),
)

SOCIAL_MEDIA_PLATFORM_DOMAINS = {
    "INSTAGRAM": ("instagram.com",),
    "FACEBOOK": ("facebook.com", "fb.com", "fb.watch"),
    "TIKTOK": ("tiktok.com",),
    "YOUTUBE": ("youtube.com", "youtu.be"),
    "LINKEDIN": ("linkedin.com",),
    "X": ("x.com", "twitter.com"),
}

_url_validator = URLValidator(schemes=("http", "https"))


def _external_hostname(value):
    try:
        parsed = urlsplit(str(value).strip())
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port  # Trigger validation of malformed ports.
    except (TypeError, ValueError):
        raise ValidationError("Enter a valid external HTTP or HTTPS URL.") from None

    if port == 0:
        raise ValidationError("Enter a valid external HTTP or HTTPS URL.")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValidationError("Only HTTP and HTTPS links are allowed.")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValidationError("Enter a valid external HTTP or HTTPS URL.")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".intranet")):
        raise ValidationError("Enter a public external website, not a local network address.")

    try:
        address = ip_address(hostname)
    except ValueError:
        # Domain names must be qualified; a single local machine name is not public.
        if "." not in hostname:
            raise ValidationError("Enter a public external website, not a local network address.") from None
    else:
        if not address.is_global:
            raise ValidationError("Enter a public external website, not a local network address.")

    return hostname


def validate_external_profile_url(value):
    """Accept only syntactically valid, public HTTP(S) URLs; never perform a network lookup."""
    if value in (None, ""):
        return value
    _url_validator(value)
    _external_hostname(value)
    return value


def validate_social_media_url(value, platform):
    """Validate a social profile URL and require its host to match its selected platform."""
    if value in (None, ""):
        return value
    hostname = _external_hostname(value)
    validate_external_profile_url(value)
    allowed_domains = SOCIAL_MEDIA_PLATFORM_DOMAINS.get(platform)
    if not allowed_domains:
        raise ValidationError("Select a supported social media platform for this link.")
    if not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
        label = dict(SOCIAL_MEDIA_PLATFORM_CHOICES).get(platform, "selected platform")
        raise ValidationError(f"Enter a URL for {label}.")
    return value
