"""Durable lifecycle for outbound security emails.

One ``EmailDelivery`` row per intended email. The web request that caused the
email writes the row and, if the broker answers, hands the task to Celery on
the notifications queue. When the broker does not answer, the row stays
``queued``: the watchdog (``home.tasks.dispatch_queued_emails``, every minute
on the notifications queue) re-offers it, and once the code's usefulness
deadline passes the row is marked ``failed`` so the person waiting is told,
instead of the page saying "check your email" forever.

No SMTP socket is ever opened inside a web request on this path.
"""

import logging
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .email_errors import (
    EMAIL_MAX_RETRIES,
    PERMANENT,
    classify_email_exception,
    describe_email_error,
    retry_countdown,
)
from .models import EmailDelivery
from .otp_security import email_delivery_failure_key
from .security_mail import decrypt_email_code

logger = logging.getLogger(__name__)

EMAIL_FAILURE_TTL_SECONDS = 900
# How long a pushed-but-unclaimed delivery waits before the watchdog may push
# it again. Longer than a healthy worker's claim time; shorter than the OTP.
PUSH_GRACE_SECONDS = 120
# Broker pushes a single delivery may undergo before the watchdog stops trying
# (the expiry deadline stops it sooner in practice).
MAX_DISPATCH_ATTEMPTS = 12


def create_delivery(*, purpose, task_name, recipient_email, args, ttl_seconds, max_attempts=EMAIL_MAX_RETRIES):
    """Record one intended email. Returns the row (status ``queued``)."""
    now = timezone.now()
    return EmailDelivery.objects.create(
        event_key=f"email:{uuid.uuid4().hex}",
        purpose=str(purpose),
        task_name=str(task_name)[:140],
        recipient_email=(recipient_email or "").strip()[:254],
        args=list(args),
        expires_at=now + timedelta(seconds=max(1, int(ttl_seconds))),
        max_attempts=max_attempts,
    )


def push_delivery(delivery, task):
    """Offer the delivery to Celery once. Returns (pushed: bool, error: str).

    Never raises: a broker outage must not become an exception in whatever
    context this was called from.
    """
    try:
        # Queue placement comes from CELERY_TASK_ROUTES (every security-code
        # task routes to the dedicated "notifications" worker).
        task.delay(*list(delivery.args), delivery_id=str(delivery.id))
    except Exception as exc:  # kombu/redis/socket: anything the broker layer raises
        delivery.dispatch_attempts = delivery.dispatch_attempts + 1
        delivery.available_at = timezone.now() + timedelta(seconds=30)
        delivery.save(update_fields=["dispatch_attempts", "available_at", "updated_at"])
        return False, describe_email_error(exc)
    delivery.dispatch_attempts = delivery.dispatch_attempts + 1
    delivery.available_at = timezone.now() + timedelta(seconds=PUSH_GRACE_SECONDS)
    delivery.save(update_fields=["dispatch_attempts", "available_at", "updated_at"])
    return True, ""


def _flag_failure(recipient_email):
    if not recipient_email:
        return
    try:
        cache.set(email_delivery_failure_key(recipient_email), True, timeout=EMAIL_FAILURE_TTL_SECONDS)
    except Exception:  # pragma: no cover - a cache blip must not mask the failure itself
        logger.warning("email_failure_flag_not_stored", exc_info=True)


def _clear_failure(recipient_email):
    if not recipient_email:
        return
    try:
        cache.delete(email_delivery_failure_key(recipient_email))
    except Exception:  # pragma: no cover
        logger.warning("email_failure_flag_not_cleared", exc_info=True)


def due_deliveries(*, batch_size=50):
    now = timezone.now()
    return list(
        EmailDelivery.objects.filter(
            status=EmailDelivery.Status.QUEUED,
            available_at__lte=now,
            expires_at__gt=now,
            dispatch_attempts__lt=MAX_DISPATCH_ATTEMPTS,
        ).order_by("created_at")[:batch_size]
    )


def expire_overdue_deliveries(*, recipient_email=None, now=None):
    """Mark deliveries whose useful code window has passed as ``failed``.

    Queued or retrying mail whose code has expired will never be usable, and
    a send in flight when the worker died cannot deliver anything useful
    either. Fails them loudly and records the failure so the waiting person
    is told instead of left hanging. Returns the number expired.
    """
    now = now or timezone.now()
    queryset = EmailDelivery.objects.filter(
        status__in=(EmailDelivery.Status.QUEUED, EmailDelivery.Status.RETRYING, EmailDelivery.Status.SENDING),
        expires_at__lte=now,
    )
    if recipient_email:
        queryset = queryset.filter(recipient_email=recipient_email)
    expired = 0
    for delivery in queryset.only("id", "recipient_email", "status"):
        delivery.status = EmailDelivery.Status.FAILED
        delivery.last_error = "expired_code"
        delivery.save(update_fields=["status", "last_error", "updated_at"])
        _flag_failure(delivery.recipient_email)
        expired += 1
    if expired:
        logger.error("email_delivery_expired count=%s (codes were no longer valid; resend to get a fresh one)", expired)
    return expired


def delivery_pending(recipient_email):
    """True while at least one delivery for this address is in flight."""
    now = timezone.now()
    return EmailDelivery.objects.filter(
        recipient_email=recipient_email,
        status__in=(EmailDelivery.Status.QUEUED, EmailDelivery.Status.SENDING, EmailDelivery.Status.RETRYING),
        expires_at__gt=now,
    ).exists()


def run_code_email_task(task, *, delivery_id, recipient, encrypted_code, purpose, send, result_purpose=None):
    """Shared lifecycle for the security-code tasks.

    ``send(otp_code)`` performs the one SMTP send. The caller owns building the
    message; this function owns the durable state machine, the bounded ladder
    and the failure visibility. Returns a small dict for the task result.
    """
    now = timezone.now()
    delivery = EmailDelivery.objects.filter(pk=delivery_id).first() if delivery_id else None
    if delivery is not None and delivery.recipient_email != recipient and recipient:
        # A staff task learns the recipient only after loading the challenge.
        delivery.recipient_email = recipient
        delivery.save(update_fields=["recipient_email", "updated_at"])

    if delivery is not None and now >= delivery.expires_at:
        logger.error(
            "email_delivery_skipped purpose=%s reason=expired (code was no longer valid; not sent)",
            purpose,
        )
        delivery.status = EmailDelivery.Status.FAILED
        delivery.last_error = "expired_code"
        delivery.save(update_fields=["status", "last_error", "updated_at"])
        _flag_failure(recipient)
        return {"sent": False, "reason": "expired"}

    attempts = delivery.attempts + 1 if delivery is not None else task.request.retries + 1
    if delivery is not None:
        delivery.status = EmailDelivery.Status.SENDING
        delivery.attempts = attempts
        delivery.save(update_fields=["status", "attempts", "updated_at"])

    try:
        otp_code = decrypt_email_code(encrypted_code)
    except Exception as exc:  # ciphertext that cannot be decrypted never heals
        error = describe_email_error(exc)
        logger.error("email_delivery_failed task=%s purpose=%s error=%s (ciphertext invalid)", task.name, purpose, error)
        if delivery is not None:
            delivery.status = EmailDelivery.Status.FAILED
            delivery.last_error = error[:160]
            delivery.save(update_fields=["status", "last_error", "updated_at"])
        _flag_failure(recipient)
        return {"sent": False, "reason": "permanent"}

    try:
        send(otp_code)
    except Exception as exc:
        error = describe_email_error(exc)
        kind = classify_email_exception(exc)
        countdown = retry_countdown(attempts)
        # Never retry past the useful life of the code, and never past the ladder.
        useful = (delivery is None) or (timezone.now() + timedelta(seconds=countdown) < delivery.expires_at)
        max_attempts = delivery.max_attempts if delivery is not None else EMAIL_MAX_RETRIES
        if kind != PERMANENT and attempts <= max_attempts and useful:
            if delivery is not None:
                delivery.status = EmailDelivery.Status.RETRYING
                delivery.last_error = error[:160]
                delivery.available_at = timezone.now() + timedelta(seconds=countdown)
                delivery.save(update_fields=["status", "last_error", "available_at", "updated_at"])
            logger.warning(
                "email_delivery_retrying task=%s purpose=%s attempt=%s countdown=%ss error=%s",
                task.name, purpose, attempts, countdown, error,
            )
            raise task.retry(exc=exc, countdown=countdown)
        logger.error(
            "email_delivery_failed task=%s purpose=%s error=%s attempts=%s (check `python manage.py check_email`)",
            task.name, purpose, error, attempts,
        )
        if delivery is not None:
            delivery.status = EmailDelivery.Status.FAILED
            delivery.last_error = error[:160]
            delivery.save(update_fields=["status", "last_error", "updated_at"])
        _flag_failure(recipient)
        return {"sent": False, "reason": "permanent" if kind == PERMANENT else "retries_exhausted"}

    if delivery is not None:
        delivery.status = EmailDelivery.Status.SENT
        delivery.sent_at = timezone.now()
        delivery.last_error = ""
        delivery.save(update_fields=["status", "sent_at", "last_error", "updated_at"])
    # Success wipes any earlier failure marker for this address and purpose path:
    # a resent code that lands must not leave the page shouting about the old failure.
    _clear_failure(recipient)
    logger.info("email_delivery_sent task=%s purpose=%s attempt=%s", task.name, purpose, attempts)
    return {"sent": True, "purpose": result_purpose or purpose}
