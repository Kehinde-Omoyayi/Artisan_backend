"""Secure support access for registered platform users, including blocked ones.

A blocked customer or artisan cannot use the ordinary login flow, yet they must
still be able to appeal. This module proves control of the *registered and
already verified* email address with a one-time code, then issues a short-lived
access token that is only honoured by the support case creation endpoint.

Rules enforced here:

* the account must exist and its email must already be verified;
* the code expires, has attempt limits, is single use and is never returned;
* a cooldown and the API throttle limit how often a code can be requested;
* the response is identical whether or not the address exists (no enumeration);
* the identity attached to a case is always resolved on the server.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from .models import SupportAccessGrant

SUPPORT_ACCESS_HEADER = "HTTP_X_SUPPORT_ACCESS_TOKEN"
SUPPORT_ACCESS_TOKEN_HEADER_NAME = "X-Support-Access-Token"

CODE_TTL_MINUTES = int(getattr(settings, "SUPPORT_ACCESS_CODE_MINUTES", 15))
SESSION_TTL_HOURS = int(getattr(settings, "SUPPORT_ACCESS_SESSION_HOURS", 6))
MAX_CODE_ATTEMPTS = int(getattr(settings, "SUPPORT_ACCESS_MAX_ATTEMPTS", 5))
REQUEST_COOLDOWN_SECONDS = int(getattr(settings, "SUPPORT_ACCESS_COOLDOWN", 60))

GENERIC_ACCEPTED = {
    "status": "code_sent",
    "message": (
        "If that address belongs to a verified Artisan Market account, a "
        "one-time access code is on its way. It expires in "
        f"{CODE_TTL_MINUTES} minutes."
    ),
    "expires_in": CODE_TTL_MINUTES * 60,
}


def normalize_email(value):
    return str(value or "").strip().casefold()


def _digest(label, value):
    key = str(settings.SECRET_KEY).encode()
    return hmac.new(key, f"{label}\0{value}".encode(), hashlib.sha256).hexdigest()


def _cooldown_key(email):
    return f"support-access-cooldown:{_digest('email', email)}"


def _throttle_key(email):
    return f"support-access-requests:{_digest('email', email)}"


def eligible_user_for_email(email):
    """The account that may use the protected support process, if any.

    Blocked, locked or suspended accounts stay eligible: being blocked is
    exactly why someone needs to appeal. Unverified addresses are not.
    """
    email = normalize_email(email)
    if not email:
        return None
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).order_by("id").first()
    if user is None or not user.email_verified:
        return None
    if user.is_superuser and not user.is_staff:
        return None
    return user


def account_type_for(user):
    if user is None:
        return ""
    if getattr(user, "artisan_profile", None) is not None:
        return "artisan"
    if getattr(user, "customer_profile", None) is not None:
        return "customer"
    return ""


def masked_email(email):
    email = normalize_email(email)
    if "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    head = local[:2]
    return f"{head}{'•' * max(2, len(local) - 2)}@{domain}"


def _ip_hash(request):
    if request is None:
        return ""
    try:
        from risk.network import get_client_ip, hash_ip

        return hash_ip(get_client_ip(request))
    except Exception:
        return ""


def request_support_access_code(*, email, request=None, user_agent=""):
    """Send a one-time code, or stay silent. Never reveals whether the address exists."""
    normalized = normalize_email(email)
    if not normalized:
        return dict(GENERIC_ACCEPTED)

    # Rate limits: a short cooldown between sends plus a rolling hourly cap.
    if cache.get(_cooldown_key(normalized)):
        return dict(GENERIC_ACCEPTED)
    attempts = cache.get_or_set(_throttle_key(normalized), 0, timeout=3600)
    if int(attempts) >= int(getattr(settings, "SUPPORT_ACCESS_MAX_REQUESTS_PER_HOUR", 5)):
        return dict(GENERIC_ACCEPTED)
    cache.incr(_throttle_key(normalized))

    user = eligible_user_for_email(normalized)
    if user is None:
        # Burn the cooldown anyway so timing and behaviour stay identical.
        cache.set(_cooldown_key(normalized), 1, timeout=REQUEST_COOLDOWN_SECONDS)
        return dict(GENERIC_ACCEPTED)

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    SupportAccessGrant.objects.filter(
        email=normalized, consumed_at__isnull=True, session_token_hash=""
    ).update(
        consumed_at=timezone.now(),
        session_revoked_at=timezone.now(),
    )
    grant = SupportAccessGrant.objects.create(
        user=user,
        email=normalized,
        purpose=SupportAccessGrant.Purpose.APPEAL,
        code_hash=_make_code_hash(code),
        ip_hash=_ip_hash(request),
        user_agent=str(user_agent or "")[:300],
        code_expires_at=timezone.now() + timedelta(minutes=CODE_TTL_MINUTES),
    )
    _send_code(user, code)
    cache.set(_cooldown_key(normalized), 1, timeout=REQUEST_COOLDOWN_SECONDS)
    return {
        **GENERIC_ACCEPTED,
        "masked_email": masked_email(normalized),
        "grant_id": str(grant.pk),
    }


def _make_code_hash(code):
    from django.contrib.auth.hashers import make_password

    return make_password(str(code))


def _check_code_hash(code, encoded):
    from django.contrib.auth.hashers import check_password

    return check_password(str(code), encoded)


def _send_code(user, code):
    from home.security_mail import encrypt_email_code
    from home.tasks import dispatch_email, send_otp_email

    try:
        dispatch_email(send_otp_email, user.first_name, user.email, encrypt_email_code(code), purpose="otp", ttl_seconds=300)
        return True
    except Exception:  # pragma: no cover - broker outage must not break access
        import logging

        logging.getLogger(__name__).exception("support_access_email_enqueue_failed")
        return False


def verify_support_access_code(*, email, code, request=None):
    """Exchange a valid code for a short-lived support access token."""
    from rest_framework.exceptions import ValidationError

    normalized = normalize_email(email)
    error = ValidationError(
        {"code": "That code is not valid or has expired. Request a new one."}
    )
    grant = (
        SupportAccessGrant.objects.select_for_update(of=("self",))
        .filter(email=normalized, consumed_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if grant is None:
        raise error
    if grant.attempts >= MAX_CODE_ATTEMPTS:
        grant.consumed_at = grant.consumed_at or timezone.now()
        grant.save(update_fields=["consumed_at", "updated_at"])
        raise ValidationError(
            {"code": "Too many incorrect attempts. Request a new access code."}
        )
    if grant.code_expires_at <= timezone.now():
        grant.consumed_at = timezone.now()
        grant.save(update_fields=["consumed_at", "updated_at"])
        raise error
    if not _check_code_hash(str(code or "").strip(), grant.code_hash):
        grant.attempts += 1
        grant.save(update_fields=["attempts", "updated_at"])
        raise error

    token = secrets.token_urlsafe(48)
    now = timezone.now()
    grant.consumed_at = now
    grant.session_token_hash = hashlib.sha256(token.encode()).hexdigest()
    grant.session_expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    grant.ip_hash = grant.ip_hash or _ip_hash(request)
    grant.save(
        update_fields=[
            "consumed_at",
            "session_token_hash",
            "session_expires_at",
            "ip_hash",
            "updated_at",
        ]
    )
    return {
        "access_token": token,
        "token_header": SUPPORT_ACCESS_TOKEN_HEADER_NAME,
        "expires_at": grant.session_expires_at,
        "expires_in": int((grant.session_expires_at - now).total_seconds()),
        "account_type": account_type_for(grant.user),
        "masked_email": masked_email(grant.user.email),
        "message": (
            "Your identity is confirmed. Submit your appeal; it is attached to "
            "your account automatically."
        ),
    }


def resolve_support_access_token(token):
    """Return the live grant for a token, or None. Never raises."""
    token = str(token or "").strip()
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    grant = (
        SupportAccessGrant.objects.select_related("user")
        .filter(
            session_token_hash=digest,
            session_revoked_at__isnull=True,
            session_expires_at__gt=timezone.now(),
            consumed_at__isnull=False,
        )
        .first()
    )
    return grant


def revoke_support_access(grant):
    if grant is None:
        return
    grant.session_revoked_at = timezone.now()
    grant.session_expires_at = min(
        grant.session_expires_at or timezone.now(), timezone.now()
    )
    grant.save(update_fields=["session_revoked_at", "session_expires_at", "updated_at"])
