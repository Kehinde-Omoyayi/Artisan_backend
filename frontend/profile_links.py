"""Safe HTTP(S) validation for optional public artisan profile links."""
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
_URL_VALIDATOR = URLValidator(schemes=("http", "https"))


def validate_external_profile_url(value):
    if value in (None, ""):
        return value
    _URL_VALIDATOR(value)
    try:
        parsed = urlsplit(str(value).strip())
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except (TypeError, ValueError):
        raise ValidationError("Enter a valid external HTTP or HTTPS URL.") from None
    if port == 0:
        raise ValidationError("Enter a valid external HTTP or HTTPS URL.")
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
        raise ValidationError("Enter a valid external HTTP or HTTPS URL.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".intranet")) or "." not in host:
        raise ValidationError("Enter a public external website, not a local network address.")
    try:
        address = ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValidationError("Enter a public external website, not a local network address.")
    return value


def validate_social_media_url(value, platform):
    if not value:
        return value
    validate_external_profile_url(value)
    host = urlsplit(value.strip()).hostname.lower().rstrip(".")
    domains = SOCIAL_MEDIA_PLATFORM_DOMAINS.get(platform)
    if not domains:
        raise ValidationError("Select a supported social media platform for this link.")
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        label = dict(SOCIAL_MEDIA_PLATFORM_CHOICES).get(platform, "selected platform")
        raise ValidationError(f"Enter a URL for {label}.")
    return value


def clean_profile_links(form, cleaned_data):
    for index in (1, 2):
        platform_field = f"social_media_{index}_platform"
        url_field = f"social_media_{index}_url"
        platform = cleaned_data.get(platform_field, "")
        url = cleaned_data.get(url_field, "").strip()
        if bool(platform) != bool(url):
            form.add_error(url_field, "Choose both a platform and its profile URL, or leave both blank.")
        elif url:
            try:
                validate_social_media_url(url, platform)
            except ValidationError as exc:
                form.add_error(url_field, exc.messages)
            else:
                cleaned_data[url_field] = url
    portfolio_url = cleaned_data.get("portfolio_url", "").strip()
    if portfolio_url:
        try:
            validate_external_profile_url(portfolio_url)
        except ValidationError as exc:
            form.add_error("portfolio_url", exc.messages)
        else:
            cleaned_data["portfolio_url"] = portfolio_url
    return cleaned_data
