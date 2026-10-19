import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .email_templates import send_security_code_email
from .manager_snapshots import (
    acquire_due_snapshot,
    claim_snapshot_run,
    complete_snapshot_run,
    fail_snapshot_run,
    record_snapshot_dispatch_failure,
)

logger = logging.getLogger(__name__)
User = get_user_model()


_EMAIL_RETRY_KWARGS = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 300,
    "retry_jitter": True,
    "max_retries": 5,
}


@shared_task(**_EMAIL_RETRY_KWARGS)
def send_verification_email(first_name, email, encrypted_code):
    from .security_mail import decrypt_email_code

    otp_code = decrypt_email_code(encrypted_code)
    send_security_code_email(
        recipient_email=email,
        recipient_name=first_name,
        subject="Verify Your Artisan Platform Account",
        heading="Verify your email address",
        intro="Enter this one-time code in the Artisan Market verification screen to confirm the email address on your account.",
        code=otp_code,
        code_label="EMAIL VERIFICATION CODE",
        purpose_label="Email verification",
        security_note="If you did not start this account verification, ignore this email and do not share the code.",
    )
    return {"sent": True, "purpose": "verification"}


@shared_task(**_EMAIL_RETRY_KWARGS)
def send_password_reset_email(first_name, email, encrypted_code):
    from .security_mail import decrypt_email_code

    otp_code = decrypt_email_code(encrypted_code)
    send_security_code_email(
        recipient_email=email,
        recipient_name=first_name,
        subject="Password Reset Verification Code",
        heading="Reset your password",
        intro="Use this one-time code to continue resetting the password for your Artisan Market account.",
        code=otp_code,
        code_label="PASSWORD RESET CODE",
        purpose_label="Password reset",
        security_note="If you did not request this reset, ignore this email. Your password will not change unless the code is used.",
    )
    return {"sent": True, "purpose": "password_reset"}


@shared_task(**_EMAIL_RETRY_KWARGS)
def send_otp_email(first_name, email, encrypted_code):
    from .security_mail import decrypt_email_code

    otp_code = decrypt_email_code(encrypted_code)
    send_security_code_email(
        recipient_email=email,
        recipient_name=first_name,
        subject="Your Artisan Platform Verification Code",
        heading="Complete your verification",
        intro="Enter this one-time code in the verification screen you opened to continue securely.",
        code=otp_code,
        code_label="VERIFICATION CODE",
        purpose_label="Account verification",
        security_note="If you did not request a code, ignore this email and do not share it with anyone.",
    )
    return {"sent": True, "purpose": "otp"}


@shared_task(**_EMAIL_RETRY_KWARGS)
def send_staff_security_code(challenge_id, encrypted_code):
    """Deliver a short-lived staff code without placing plaintext secrets in the broker."""
    from .models import StaffSecurityChallenge
    from .security_mail import decrypt_email_code

    challenge = StaffSecurityChallenge.objects.select_related("user").filter(
        pk=challenge_id,
        consumed_at__isnull=True,
        invalidated_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if challenge is None:
        return {"sent": False, "purpose": "staff_recovery"}

    purpose = challenge.purpose
    site_url = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
    otp_code = decrypt_email_code(encrypted_code)
    if purpose == "LOGIN_MFA":
        purpose_label = "staff sign-in"
        flow_path = "/auth/staff-mfa/"
        subject = "Your staff sign-in verification code"
        heading = "Confirm your staff sign-in"
        intro = "A sign-in attempt for your staff account needs a second verification step."
        security_note = "If you did not attempt to sign in, do not share this code and contact your platform manager."
    else:
        is_setup = purpose == "SECURITY_SETUP"
        purpose_label = "security-question setup" if is_setup else "password recovery"
        flow_path = "/auth/staff-security/setup/" if is_setup else "/auth/staff-password-reset/"
        subject = "Staff account verification code"
        heading = "Verify staff account security" if is_setup else "Recover your staff account"
        intro = f"A manager initiated {purpose_label} for your staff account. Use this code to continue."
        security_note = "If you did not expect this request, do not share the code and contact your platform manager."
    send_security_code_email(
        recipient_email=challenge.user.email,
        recipient_name=challenge.user.first_name,
        subject=subject,
        heading=heading,
        intro=intro,
        code=otp_code,
        code_label="STAFF SECURITY CODE",
        purpose_label="Staff account security",
        security_note=security_note,
        action_url=f"{site_url}{flow_path}" if site_url else "",
        action_label="Continue securely",
    )
    return {"sent": True, "purpose": purpose_label}


@shared_task
def delete_unverified_accounts():
    """Delete accounts that never completed email verification.

    Only accounts that never logged in (so they were never verified) are removed, and each is deleted on its own so
    one account with protected related rows can never abort the whole run or cascade into financial history.
    """
    from django.db.models import ProtectedError

    cutoff = timezone.now() - timedelta(days=7)
    candidates = User.objects.filter(
        is_active=False,
        security_status="NORMAL",
        is_staff=False,
        is_superuser=False,
        last_login__isnull=True,
        created_at__lte=cutoff,
    ).values_list("pk", flat=True)
    deleted = 0
    for pk in list(candidates[:1000]):
        try:
            with transaction.atomic():
                deleted += User.objects.filter(pk=pk, is_active=False, last_login__isnull=True).delete()[0] > 0
        except ProtectedError:
            logger.warning("Skipped unverified account %s: it has protected related records", pk)
    logger.info("Deleted %s stale unverified accounts", deleted)
    return deleted


@shared_task
def purge_expired_staff_security_challenges(batch_size=5000):
    """Delete expired staff MFA/recovery challenges after the configured operational window."""
    from .models import StaffSecurityChallenge

    retention_days = int(getattr(settings, "STAFF_SECURITY_CHALLENGE_RETENTION_DAYS", 30))
    cutoff = timezone.now() - timedelta(days=max(1, retention_days))
    batch_size = max(1, min(int(batch_size), 20000))
    stale_ids = list(
        StaffSecurityChallenge.objects.filter(expires_at__lt=cutoff)
        .order_by("expires_at")
        .values_list("pk", flat=True)[:batch_size]
    )
    if not stale_ids:
        return {"deleted": 0}
    deleted, _details = StaffSecurityChallenge.objects.filter(
        pk__in=stale_ids,
        expires_at__lt=cutoff,
    ).delete()
    return {"deleted": deleted}


@shared_task
def dispatch_manager_snapshot_if_due():
    """Celery-beat entry point: enqueue one DB-claimed snapshot period when due."""
    run_id, state = acquire_due_snapshot()
    if not run_id:
        return {"status": state}
    try:
        generate_manager_analytics_snapshot.apply_async(args=[run_id])
    except Exception as exc:
        record_snapshot_dispatch_failure(run_id, exc)
        logger.exception("Could not dispatch manager analytics snapshot run %s", run_id)
        return {"status": "dispatch_failed", "run_id": run_id}
    return {"status": "dispatched", "run_id": run_id}


@shared_task
def generate_manager_analytics_snapshot(run_id):
    """Build and persist one aggregate snapshot; duplicate task deliveries are idempotent."""
    run = claim_snapshot_run(run_id)
    if run is None:
        return {"status": "already_claimed_or_finished", "run_id": str(run_id)}
    try:
        from .manager_analytics import build_manager_overview

        payload = build_manager_overview(now=timezone.now())
        complete_snapshot_run(run_id, payload)
    except Exception as exc:
        fail_snapshot_run(run_id, exc)
        logger.exception("Manager analytics snapshot run %s failed", run_id)
        raise
    return {"status": "succeeded", "run_id": str(run_id)}
