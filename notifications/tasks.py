import logging
import uuid

from celery import shared_task
from redis.exceptions import RedisError
from home.email_brand import BrandedEmailMultiAlternatives as EmailMultiAlternatives
from django.template.loader import render_to_string

from core.exceptions import PermanentTaskError
from home.email_errors import (
    EMAIL_MAX_RETRIES,
    PERMANENT,
    classify_email_exception,
    describe_email_error,
    retry_countdown,
)

from .email_backend import (
    acquire_lock,
    already_sent,
    mark_sent,
    release_lock,
)

logger = logging.getLogger(__name__)


def _redis_retry_or_raise(task, exc, event_key):
    """Redis trouble is transient, but not infinitely so."""
    attempts = task.request.retries + 1
    if task.request.retries < EMAIL_MAX_RETRIES:
        raise task.retry(exc=exc, countdown=retry_countdown(attempts))
    logger.error("notification_redis_unavailable event=%s error=%s - giving up to the outbox", event_key, describe_email_error(exc))
    raise exc


@shared_task(
    bind=True,
    max_retries=EMAIL_MAX_RETRIES,
)
def send_email_notification(
    self,
    *,
    event_key,
    recipient_email,
    recipient_name,
    subject,
    message,
    action_url=None,
    action_label=None,
    attachments=None,
):
    if not recipient_email:
        logger.warning(
            "Notification skipped: recipient has no email. event=%s",
            event_key,
        )
        return {"status": "skipped", "reason": "missing_email"}

    try:
        duplicate = already_sent(event_key)
    except RedisError as exc:
        return _redis_retry_or_raise(self, exc, event_key)
    if duplicate:
        logger.info(
            "Duplicate notification skipped. event=%s",
            event_key,
        )
        return {"status": "duplicate"}

    token = str(uuid.uuid4())

    try:
        acquired = acquire_lock(event_key, token)
    except RedisError as exc:
        return _redis_retry_or_raise(self, exc, event_key)
    if not acquired:
        logger.info(
            "Notification already being processed. event=%s",
            event_key,
        )
        return {"status": "locked"}

    try:
        try:
            duplicate = already_sent(event_key)
        except RedisError:
            duplicate = False
        if duplicate:
            return {"status": "duplicate"}

        context = {
            "recipient_name": recipient_name or "there",
            "subject": subject,
            "message": message,
            "action_url": action_url,
            "action_label": action_label,
        }

        text_body = render_to_string(
            "emails/notification.txt",
            context,
        ).strip()

        html_body = render_to_string(
            "emails/notification.html",
            context,
        )

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=None,
            to=[recipient_email],
        )

        email.attach_alternative(html_body, "text/html")
        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue
            if attachment.get("kind") == "payout_receipt_pdf":
                from payments.receipts import payout_receipt_pdf
                content = payout_receipt_pdf(attachment["payment_id"])
                filename = str(attachment.get("filename") or "payout-receipt.pdf")
                if not filename.lower().endswith(".pdf"):
                    filename = "payout-receipt.pdf"
                email.attach(filename, content, "application/pdf")
            elif attachment.get("content_base64"):
                import base64
                content = base64.b64decode(attachment["content_base64"], validate=True)
                filename = str(attachment.get("filename") or "attachment.bin")
                content_type = str(attachment.get("content_type") or "application/octet-stream")
                email.attach(filename, content, content_type)
        try:
            email.send(fail_silently=False)
        except Exception as exc:
            kind = classify_email_exception(exc)
            error = describe_email_error(exc)
            attempts = self.request.retries + 1
            if kind != PERMANENT and self.request.retries < EMAIL_MAX_RETRIES:
                logger.warning(
                    "notification_email_retrying event=%s recipient=%s attempt=%s countdown=%ss error=%s",
                    event_key,
                    recipient_email,
                    attempts,
                    retry_countdown(attempts),
                    error,
                )
                raise self.retry(exc=exc, countdown=retry_countdown(attempts)) from exc
            logger.error(
                "notification_email_failed event=%s recipient=%s error=%s attempts=%s",
                event_key,
                recipient_email,
                error,
                attempts,
            )
            if kind == PERMANENT:
                # Dead-letter immediately: re-queueing an impossible send only hides the failure.
                raise PermanentTaskError(error) from exc
            # Transient, ladder exhausted: let the outbox apply its own slower backoff.
            raise
        try:
            mark_sent(event_key)
        except RedisError:
            # The mail is on its way; losing the dedupe marker only risks a rare duplicate.
            logger.warning("notification_mark_sent_failed event=%s (sent; dedupe marker lost)", event_key)

        logger.info(
            "Notification email sent successfully. "
            "event=%s recipient=%s",
            event_key,
            recipient_email,
        )

        return {"status": "sent"}

    finally:
        try:
            release_lock(event_key, token)
        except RedisError:
            logger.warning("notification_lock_release_failed event=%s", event_key)


FANOUT_MAX_RECIPIENTS = 50


@shared_task(
    bind=True,
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
)
def fanout_service_request(self, service_request_id, variant="created"):
    from django.contrib.auth import get_user_model

    from services.models import ServiceRequest
    from .matching import get_matching_artisans
    from .events import notify_service_request_no_match

    try:
        service_request = (
            ServiceRequest.objects
            .select_related("requester", "category", "location")
            .get(pk=service_request_id)
        )
    except ServiceRequest.DoesNotExist:
        logger.warning(
            "Service request %s no longer exists.",
            service_request_id,
        )
        return {"status": "not_found"}

    kind = variant.split(":", 1)[0]
    if kind in {"created", "updated"} and service_request.status != ServiceRequest.Status.OPEN:
        return {"status": "closed"}
    artisans = get_matching_artisans(service_request)

    if not artisans:
        logger.info(
            "No matching artisans found for service request %s",
            service_request_id,
        )
        if kind == "created":
            notify_service_request_no_match(service_request)
        return {"status": "no_match"}

    from .events import (
        notify_artisan_service_demand,
        notify_artisan_service_request_changed,
        notify_artisan_service_request_withdrawn,
    )

    notify = {
        "created": notify_artisan_service_demand,
        "updated": notify_artisan_service_request_changed,
        "withdrawn": notify_artisan_service_request_withdrawn,
    }.get(kind, notify_artisan_service_demand)

    seen_users = set()
    task_count = 0
    for artisan_service in artisans.iterator():
        user = artisan_service.artisan.user
        if user.id in seen_users or not user.is_active:
            continue
        seen_users.add(user.id)
        notify(service_request, user, revision=variant)
        task_count += 1

    return {
        "status": "queued",
        "variant": variant,
        "recipients": task_count,
    }