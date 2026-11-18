"""Django-admin MFA state, bound to the authenticated staff identity in the browser session."""

from django.conf import settings
from django.utils import timezone

from .models import StaffSecurityChallenge
from .staff_mfa import begin_staff_mfa_challenge, mask_staff_email

ADMIN_MFA_PENDING_KEYS = (
    "admin_mfa_challenge_id",
    "admin_mfa_masked_email",
    "admin_mfa_resend_after",
)


def clear_admin_mfa_pending(session):
    for key in ADMIN_MFA_PENDING_KEYS:
        session.pop(key, None)


def admin_mfa_is_current(session, user, *, now=None):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if str(session.get("admin_mfa_user_id", "")) != str(user.pk):
        return False
    try:
        authenticated_at = int(session.get("admin_mfa_authenticated_at"))
    except (TypeError, ValueError):
        return False
    now = now or timezone.now()
    age = int(now.timestamp()) - authenticated_at
    max_age = int(getattr(settings, "STAFF_MFA_MAX_AGE_SECONDS", 8 * 60 * 60))
    return 0 <= age <= max_age


def ensure_admin_mfa_challenge(request):
    """Return a live login-MFA challenge bound to this staff session, creating one when needed."""
    challenge_id = request.session.get("admin_mfa_challenge_id")
    if challenge_id:
        challenge = StaffSecurityChallenge.objects.filter(
            pk=challenge_id,
            user=request.user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).first()
        if challenge is not None:
            return challenge

    clear_admin_mfa_pending(request.session)
    challenge, retry_after, _sent = begin_staff_mfa_challenge(
        user=request.user,
        request=request,
        method="django_admin",
    )
    request.session.cycle_key()
    request.session["admin_mfa_challenge_id"] = str(challenge.pk)
    request.session["admin_mfa_masked_email"] = mask_staff_email(request.user.email)
    request.session["admin_mfa_resend_after"] = int(retry_after)
    request.session.modified = True
    return challenge
