import logging
import uuid

from celery import shared_task
from redis.exceptions import RedisError
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .email_backend import (
    acquire_lock,
    already_sent,
    mark_sent,
    release_lock,
)

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(RedisError,),
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
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

    if already_sent(event_key):
        logger.info(
            "Duplicate notification skipped. event=%s",
            event_key,
        )
        return {"status": "duplicate"}

    token = str(uuid.uuid4())

    if not acquire_lock(event_key, token):
        logger.info(
            "Notification already being processed. event=%s",
            event_key,
        )
        return {"status": "locked"}

    try:
        if already_sent(event_key):
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
        email.send(fail_silently=False)

        mark_sent(event_key)

        logger.info(
            "Notification email sent successfully. "
            "event=%s recipient=%s",
            event_key,
            recipient_email,
        )

        return {"status": "sent"}

    except Exception as exc:
        logger.exception(
            "Notification email failed. "
            "event=%s recipient=%s",
            event_key,
            recipient_email,
        )

        raise self.retry(exc=exc) from exc

    finally:
        release_lock(event_key, token)


FANOUT_MAX_RECIPIENTS = 50


@shared_task(
    bind=True,
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
)
def fanout_service_request(self, service_request_id):
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

    artisans = get_matching_artisans(service_request)

    if not artisans:
        logger.info(
            "No matching artisans found for service request %s",
            service_request_id,
        )
        notify_service_request_no_match(service_request)
        return {"status": "no_match"}

    from .events import notify_artisan_service_demand

    seen_users = set()
    task_count = 0
    for artisan_service in artisans[:FANOUT_MAX_RECIPIENTS * 3]:
        user = artisan_service.artisan.user
        if user.id in seen_users or not user.is_active:
            continue
        seen_users.add(user.id)
        notify_artisan_service_demand(service_request, user)
        task_count += 1
        if task_count >= FANOUT_MAX_RECIPIENTS:
            break

    return {
        "status": "queued",
        "recipients": task_count,
    }