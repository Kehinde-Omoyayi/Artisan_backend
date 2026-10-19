"""Marketplace JWT issuance and absolute authentication-age validation."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.tokens import RefreshToken

from .password_policy import is_staff_account


DEFAULT_MARKETPLACE_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
DEFAULT_STAFF_MFA_MAX_AGE_SECONDS = 8 * 60 * 60
CLOCK_SKEW_SECONDS = 60


def marketplace_session_max_age_seconds():
    return int(getattr(settings, "MARKETPLACE_SESSION_MAX_AGE_SECONDS", DEFAULT_MARKETPLACE_SESSION_MAX_AGE_SECONDS))


def staff_mfa_max_age_seconds():
    return int(getattr(settings, "STAFF_MFA_MAX_AGE_SECONDS", DEFAULT_STAFF_MFA_MAX_AGE_SECONDS))


def issue_marketplace_tokens(user, *, staff_mfa_verified=False, authenticated_at=None):
    """Issue a version-bound refresh/access pair; staff must have completed MFA first."""
    now = authenticated_at or timezone.now()
    auth_time = int(now.timestamp())
    staff_account = is_staff_account(user)
    if staff_account and not staff_mfa_verified:
        raise ValueError("Staff tokens may only be issued after successful MFA.")

    refresh = RefreshToken.for_user(user)
    refresh["token_version"] = int(user.token_version)
    refresh["auth_time"] = auth_time
    if staff_account:
        refresh["staff_mfa"] = True
        refresh["staff_mfa_at"] = auth_time
    access = refresh.access_token
    return {
        "access": str(access),
        "refresh": str(refresh),
        "auth_time": auth_time,
        "session_version": int(user.token_version),
        "staff_mfa_at": auth_time if staff_account else None,
    }


def validate_marketplace_token_claims(token, user, *, now=None):
    """Reject legacy/malformed claims and enforce absolute user/staff session age."""
    now = now or timezone.now()
    try:
        auth_time = int(token.get("auth_time"))
    except (TypeError, ValueError):
        raise InvalidToken("Session authentication time is missing or invalid.") from None

    now_timestamp = int(now.timestamp())
    if auth_time > now_timestamp + CLOCK_SKEW_SECONDS:
        raise InvalidToken("Session authentication time is in the future.")
    if now_timestamp - auth_time > marketplace_session_max_age_seconds():
        raise InvalidToken("Session has exceeded its absolute lifetime. Sign in again.")

    if is_staff_account(user):
        try:
            staff_mfa_at = int(token.get("staff_mfa_at"))
        except (TypeError, ValueError):
            raise InvalidToken("Staff MFA proof is missing or invalid.") from None
        if token.get("staff_mfa") is not True or staff_mfa_at != auth_time:
            raise InvalidToken("Staff MFA proof is missing or invalid.")
        if staff_mfa_at > now_timestamp + CLOCK_SKEW_SECONDS:
            raise InvalidToken("Staff MFA proof is in the future.")
        if now_timestamp - staff_mfa_at > staff_mfa_max_age_seconds():
            raise InvalidToken("Staff MFA has expired. Sign in again.")
    return True
