import logging
from datetime import timedelta

from celery import shared_task
from celery.signals import task_failure
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .email_delivery import (
    EMAIL_FAILURE_TTL_SECONDS,
    create_delivery,
    due_deliveries,
    expire_overdue_deliveries,
    push_delivery,
    run_code_email_task,
)
from .email_errors import EMAIL_MAX_RETRIES
from .email_templates import send_security_code_email
from .models import EmailDelivery
from .otp_security import email_delivery_failure_key
from .manager_snapshots import (
    acquire_due_snapshot,
    claim_snapshot_run,
    complete_snapshot_run,
    fail_snapshot_run,
    record_snapshot_dispatch_failure,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# Security-code emails ride a bounded ladder (5s, 15s, 30s, 60s, 60s) that fits
# inside the five-minute validity of an OTP. Permanent failures are not retried
# at all. See home.email_errors.


def _log_email_gave_up(sender=None, task_id=None, exception=None, args=None, kwargs=None, **extra):
    """Name the failure after the last retry, so it is not just a traceback.

    Celery already logs the traceback; what is missing is a line that says which
    mail went undelivered, so support can find the person who is waiting. The
    recipient is logged because it is the only way to help them -- the code
    itself never is.
    """
    name = getattr(sender, "name", "") or ""
    if not (
        name.startswith("home.tasks.send_")
        or name in ("payments.tasks.send_payout_change_email", "manager.tasks.send_manager_step_up_code")
    ):
        return
    # Most mail tasks carry (first_name, email, encrypted_code, ...); the
    # challenge-based tasks (staff security, manager step-up) carry no address --
    # the delivery learns it from the challenge inside the task.
    recipient = ""
    if isinstance(args, (list, tuple)) and len(args) >= 2 and isinstance(args[1], str) and "@" in args[1]:
        recipient = args[1]
    logger.error(
        "email_delivery_abandoned task=%s recipient=%s error=%s "
        "(all %s retries failed; check `python manage.py check_email`)",
        name.rsplit(".", 1)[-1],
        recipient or "(unknown)",
        type(exception).__name__ if exception else "unknown",
        EMAIL_MAX_RETRIES,
    )
    if recipient:
        # Remember it where the person waiting can be told. They are sitting on
        # a screen that says "check your email"; without this the page keeps
        # saying that while nothing is on its way.
        try:
            cache.set(email_delivery_failure_key(recipient), True, timeout=EMAIL_FAILURE_TTL_SECONDS)
        except Exception:  # pragma: no cover - a cache blip must not mask the log
            logger.warning("email_failure_flag_not_stored", exc_info=True)


task_failure.connect(_log_email_gave_up, dispatch_uid="home.tasks.email_delivery_abandoned")


# Default (purpose, usefulness window in seconds) per task name. The window
# tracks the OTP lifetime for that flow: 5 minutes everywhere except the
# payout-account change, which allows 10.
_PURPOSE_DEFAULTS = {
    "home.tasks.send_verification_email": ("verification", 300),
    "home.tasks.send_password_reset_email": ("password_reset", 300),
    "home.tasks.send_otp_email": ("otp", 300),
    "home.tasks.send_staff_security_code": ("staff_security", 300),
    "payments.tasks.send_payout_change_email": ("payout_change", 600),
    "manager.tasks.send_manager_step_up_code": ("manager_step_up", 300),
}


def _recipient_from_args(args):
    for arg in args:
        if isinstance(arg, str) and "@" in arg:
            return arg
    return ""


def dispatch_email(task, *args, purpose=None, ttl_seconds=None):
    """Record the intended email durably, then hand it to Celery.

    A verification code is the difference between having an account and not,
    so the *intent* is never lost: every call writes one ``EmailDelivery`` row
    (queued) before returning. If the broker answers, the notifications worker
    delivers it; if the broker is down, the row stays queued and the watchdog
    (``dispatch_queued_emails``) re-offers it every minute until the code's
    validity window closes -- at which point the row is failed visibly and the
    person waiting sees "we could not send it" instead of waiting forever.

    No SMTP connection is opened inside the caller's request, so a
    misconfigured or unreachable mail server can never hang registration,
    login, password reset or a payout change.

    Returns one of "queued" (broker accepted) or "deferred" (recorded; the
    watchdog will retry) for the caller's log. Never raises.
    """
    task_name = getattr(task, "name", "") or str(task)
    default_purpose, default_ttl = _PURPOSE_DEFAULTS.get(task_name, ("otp", 300))
    purpose = purpose or default_purpose
    ttl_seconds = int(ttl_seconds) if ttl_seconds else default_ttl
    recipient = _recipient_from_args(args)
    try:
        delivery = create_delivery(
            purpose=purpose,
            task_name=task_name,
            recipient_email=recipient,
            args=list(args),
            ttl_seconds=ttl_seconds,
            max_attempts=EMAIL_MAX_RETRIES,
        )
    except Exception:
        # The emails table is the delivery guarantee; if it cannot be written,
        # say so as loudly as possible and let the caller carry on -- a web
        # request still must not hang on the mail server.
        logger.exception("email_queue_record_failed task=%s (nothing was recorded and nothing was sent)", task_name)
        return "deferred"
    pushed, error = push_delivery(delivery, task)
    if pushed:
        logger.info("email_queued task=%s delivery_id=%s", task_name.rsplit(".", 1)[-1], delivery.id)
        return "queued"
    logger.warning(
        "email_queue_unavailable task=%s delivery_id=%s error=%s - recorded; the watchdog will retry",
        task_name.rsplit(".", 1)[-1],
        delivery.id,
        error,
    )
    return "deferred"


@shared_task(bind=True, max_retries=EMAIL_MAX_RETRIES)
def send_verification_email(self, first_name, email, encrypted_code, delivery_id=None):
    def send(otp_code):
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

    return run_code_email_task(
        self, delivery_id=delivery_id, recipient=email,
        encrypted_code=encrypted_code, purpose="verification", send=send,
    )


@shared_task(bind=True, max_retries=EMAIL_MAX_RETRIES)
def send_password_reset_email(self, first_name, email, encrypted_code, delivery_id=None):
    def send(otp_code):
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

    return run_code_email_task(
        self, delivery_id=delivery_id, recipient=email,
        encrypted_code=encrypted_code, purpose="password_reset", send=send,
    )


@shared_task(bind=True, max_retries=EMAIL_MAX_RETRIES)
def send_otp_email(self, first_name, email, encrypted_code, delivery_id=None):
    def send(otp_code):
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

    return run_code_email_task(
        self, delivery_id=delivery_id, recipient=email,
        encrypted_code=encrypted_code, purpose="otp", send=send,
    )


@shared_task(bind=True, max_retries=EMAIL_MAX_RETRIES)
def send_staff_security_code(self, challenge_id, encrypted_code, delivery_id=None):
    """Deliver a short-lived staff code without placing plaintext secrets in the broker."""
    from .models import StaffSecurityChallenge

    challenge = StaffSecurityChallenge.objects.select_related("user").filter(
        pk=challenge_id,
        consumed_at__isnull=True,
        invalidated_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if challenge is None:
        if delivery_id:
            EmailDelivery.objects.filter(pk=delivery_id).update(
                status=EmailDelivery.Status.FAILED,
                last_error="challenge_unavailable",
            )
        return {"sent": False, "purpose": "staff_recovery"}

    purpose = challenge.purpose
    site_url = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
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
    recipient = challenge.user.email
    first_name = challenge.user.first_name

    def send(otp_code):
        send_security_code_email(
            recipient_email=recipient,
            recipient_name=first_name,
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

    return run_code_email_task(
        self, delivery_id=delivery_id, recipient=recipient,
        encrypted_code=encrypted_code, purpose="staff_security", send=send,
        result_purpose=purpose_label,
    )


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


def _task_registry():
    """Every task name the watchdog is allowed to re-offer, from one place."""
    registry = {
        task.name: task
        for task in (
            send_verification_email,
            send_password_reset_email,
            send_otp_email,
            send_staff_security_code,
        )
    }
    try:
        from payments.tasks import send_payout_change_email

        registry[send_payout_change_email.name] = send_payout_change_email
    except Exception:  # pragma: no cover - payments is always installed in this project
        logger.warning("email_watchdog_payout_task_unavailable", exc_info=True)
    try:
        from manager.tasks import send_manager_step_up_code

        registry[send_manager_step_up_code.name] = send_manager_step_up_code
    except Exception:  # pragma: no cover - manager app is always installed in this project
        logger.warning("email_watchdog_manager_task_unavailable", exc_info=True)
    return registry


@shared_task
def dispatch_queued_emails(batch_size=50):
    """Re-offer recorded-but-unsent emails to Celery. Runs every minute from beat.

    This is the recovery half of the durable queue: when the broker was down
    at dispatch time the row sat ``queued``; when a worker is back, this task
    (on the notifications queue, so a dead email worker cannot starve it) pushes
    it again. Deliveries whose codes have expired are failed by
    ``expire_overdue_deliveries`` instead, so nobody waits on a dead code.
    """
    expired = expire_overdue_deliveries()
    dispatched = 0
    failed = 0
    registry = _task_registry()
    for delivery in due_deliveries(batch_size=batch_size):
        task = registry.get(delivery.task_name)
        if task is None:
            delivery.status = EmailDelivery.Status.FAILED
            delivery.last_error = "unknown_task"
            delivery.save(update_fields=["status", "last_error", "updated_at"])
            failed += 1
            continue
        pushed, _error = push_delivery(delivery, task)
        dispatched += int(pushed)
    if expired or dispatched or failed:
        logger.info("email_watchdog expired=%s dispatched=%s failed=%s", expired, dispatched, failed)
    return {"expired": expired, "dispatched": dispatched, "failed": failed}
