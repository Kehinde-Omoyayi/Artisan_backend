"""Short-lived, single-use email MFA challenges for all staff sign-ins."""
import logging
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import Throttled

from core.audit import audit_request_action
from .models import StaffSecurityChallenge
from .password_policy import is_staff_account
from .security_mail import encrypt_email_code
from .tasks import send_staff_security_code

logger = logging.getLogger(__name__)
User = get_user_model()

STAFF_MFA_TTL = timedelta(minutes=5)
STAFF_MFA_RESEND_COOLDOWN = timedelta(seconds=60)
STAFF_MFA_MAX_CHALLENGES_PER_HOUR = 5
STAFF_MFA_MAX_ATTEMPTS = 5


def mask_staff_email(email):
    value = str(email or "").strip()
    if "@" not in value:
        return "your registered email"
    local, domain = value.rsplit("@", 1)
    visible = local[:1] if local else "*"
    return f"{visible}{'*' * min(max(len(local) - 1, 3), 8)}@{domain}"


def staff_identity_payload(user):
    return {
        "id": str(user.pk),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_staff": bool(user.is_staff),
        "is_superuser": bool(user.is_superuser),
        "role": "staff",
    }


def _queue_staff_code(challenge_id, encrypted_code):
    try:
        send_staff_security_code.delay(str(challenge_id), encrypted_code)
    except Exception:
        # Do not turn a committed login attempt into an unhandled 500, and never
        # log the email address, OTP, ciphertext or request body.
        logger.exception("staff_mfa_email_enqueue_failed challenge_id=%s", challenge_id)


def begin_staff_mfa_challenge(*, user, request=None, method="password"):
    """Create or reuse a short-lived staff challenge after primary authentication."""
    if not is_staff_account(user) or not user.is_active or user.security_status == "LOCKED":
        raise ValueError("An active staff account is required for staff MFA.")

    now = timezone.now()
    with transaction.atomic():
        locked_user = User.objects.select_for_update().filter(pk=user.pk).first()
        if (
            locked_user is None
            or not is_staff_account(locked_user)
            or not locked_user.is_active
            or locked_user.security_status == "LOCKED"
        ):
            raise ValueError("An active staff account is required for staff MFA.")

        recent = StaffSecurityChallenge.objects.filter(
            user=locked_user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            created_at__gt=now - STAFF_MFA_RESEND_COOLDOWN,
            expires_at__gt=now,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        ).order_by("-created_at").first()
        if recent:
            retry_after = max(
                1,
                int((recent.created_at + STAFF_MFA_RESEND_COOLDOWN - now).total_seconds()),
            )
            return recent, retry_after, False

        recent_count = StaffSecurityChallenge.objects.filter(
            user=locked_user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if recent_count >= STAFF_MFA_MAX_CHALLENGES_PER_HOUR:
            raise Throttled(
                wait=60,
                detail="Too many staff sign-in codes were requested. Wait before trying again.",
            )

        StaffSecurityChallenge.objects.filter(
            user=locked_user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        ).update(invalidated_at=now)

        code = f"{secrets.randbelow(900000) + 100000:06d}"
        challenge = StaffSecurityChallenge.objects.create(
            user=locked_user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            otp_hash=make_password(code),
            created_at=now,
            expires_at=now + STAFF_MFA_TTL,
        )
        encrypted_code = encrypt_email_code(code)
        transaction.on_commit(
            lambda challenge_id=challenge.pk, encrypted_code=encrypted_code: _queue_staff_code(
                challenge_id, encrypted_code
            )
        )

    if request is not None:
        audit_request_action(
            request=request,
            action="STAFF_MFA_CHALLENGE_CREATED",
            resource_type="CustomUser",
            resource_id=str(locked_user.pk),
            metadata={"method": method, "expires_in_seconds": int(STAFF_MFA_TTL.total_seconds())},
        )
    return challenge, int(STAFF_MFA_RESEND_COOLDOWN.total_seconds()), True



def verify_staff_mfa_challenge(*, challenge_id, code, request=None):
    """Atomically consume a valid, active login challenge or account one failed attempt."""
    now = timezone.now()
    with transaction.atomic():
        challenge = StaffSecurityChallenge.objects.select_for_update().filter(
            pk=challenge_id,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
            expires_at__gt=now,
        ).first()
        if challenge is None:
            return None

        user = User.objects.select_for_update().filter(
            pk=challenge.user_id,
            is_active=True,
            security_status="NORMAL",
        ).first()
        if user is None or not is_staff_account(user):
            challenge.invalidated_at = now
            challenge.save(update_fields=["invalidated_at"])
            return None

        if challenge.attempts >= STAFF_MFA_MAX_ATTEMPTS:
            challenge.invalidated_at = now
            challenge.save(update_fields=["invalidated_at"])
            return None

        if not check_password(str(code), challenge.otp_hash):
            challenge.attempts += 1
            update_fields = ["attempts"]
            if challenge.attempts >= STAFF_MFA_MAX_ATTEMPTS:
                challenge.invalidated_at = now
                update_fields.append("invalidated_at")
            challenge.save(update_fields=update_fields)
            if request is not None:
                audit_request_action(
                    request=request,
                    action="STAFF_MFA_VERIFICATION_FAILED",
                    resource_type="CustomUser",
                    resource_id=str(user.pk),
                    metadata={"attempt": challenge.attempts},
                )
            return None

        challenge.consumed_at = now
        challenge.save(update_fields=["consumed_at"])
        StaffSecurityChallenge.objects.filter(
            user=user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        ).exclude(pk=challenge.pk).update(invalidated_at=now)
        if request is not None:
            audit_request_action(
                request=request,
                action="STAFF_MFA_VERIFIED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"challenge_id": str(challenge.pk)},
            )
    return user, now

def resend_staff_mfa_challenge(*, challenge_id, request=None):
    """Issue a fresh code only from an active challenge, enforcing cooldown and hourly limits."""
    now = timezone.now()
    with transaction.atomic():
        challenge = StaffSecurityChallenge.objects.select_for_update().select_related("user").filter(
            pk=challenge_id,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
            expires_at__gt=now,
        ).first()
        if challenge is None:
            return None, 0, False

        user = User.objects.select_for_update().filter(
            pk=challenge.user_id,
            is_active=True,
            security_status="NORMAL",
        ).first()
        if user is None or not is_staff_account(user):
            return None, 0, False

        retry_at = challenge.created_at + STAFF_MFA_RESEND_COOLDOWN
        if retry_at > now:
            return challenge, max(1, int((retry_at - now).total_seconds())), False

        recent_count = StaffSecurityChallenge.objects.filter(
            user=user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if recent_count >= STAFF_MFA_MAX_CHALLENGES_PER_HOUR:
            raise Throttled(
                wait=60,
                detail="Too many staff sign-in codes were requested. Wait before trying again.",
            )

        challenge.invalidated_at = now
        challenge.save(update_fields=["invalidated_at"])
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        replacement = StaffSecurityChallenge.objects.create(
            user=user,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            otp_hash=make_password(code),
            created_at=now,
            expires_at=now + STAFF_MFA_TTL,
        )
        encrypted_code = encrypt_email_code(code)
        transaction.on_commit(
            lambda challenge_id=replacement.pk, encrypted_code=encrypted_code: _queue_staff_code(
                challenge_id, encrypted_code
            )
        )

    if request is not None:
        audit_request_action(
            request=request,
            action="STAFF_MFA_CODE_RESENT",
            resource_type="CustomUser",
            resource_id=str(user.pk),
            metadata={"expires_in_seconds": int(STAFF_MFA_TTL.total_seconds())},
        )
    return replacement, int(STAFF_MFA_RESEND_COOLDOWN.total_seconds()), True
