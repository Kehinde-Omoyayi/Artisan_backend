import logging

from celery import shared_task

from .services import expire_offers

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def expire_conversation_offers(self):
    try:
        expired = expire_offers()
        return {"status": "ok", "expired": len(expired)}
    except Exception as exc:
        logger.exception("Conversation offer expiry task failed")
        raise self.retry(exc=exc) from exc


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_conversation_reminders(self):
    from datetime import timedelta
    from django.utils import timezone
    from .events import record_conversation_event
    from .models import Conversation
    from .services import _booking_for_conversation
    from notifications.events import schedule_email

    now = timezone.now()
    window = now + timedelta(hours=24)
    sent = 0
    conversations = Conversation.objects.select_related(
        "customer", "artisan__user"
    ).filter(updated_at__gte=now - timedelta(days=30))[:1000]
    for conversation in conversations:
        booking = _booking_for_conversation(conversation)
        if not booking:
            continue
        payment = getattr(booking, "escrow_payment", None)
        if (
            payment
            and payment.status == "PAYMENT_PENDING"
            and booking.status == "ACCEPTED"
            and booking.updated_at <= now - timedelta(hours=1)
        ):
            key = f"conversation:{conversation.id}:payment-reminder:{now.date().isoformat()}"
            record_conversation_event(
                conversation=conversation,
                event_code="PAYMENT_REMINDER",
                event_key=key,
                metadata={"booking_id": str(booking.id)},
            )
            schedule_email(
                event_key=key + ":customer",
                recipient=conversation.customer,
                subject="Your booking is waiting for payment",
                message="Your booking is ready. Complete payment when you are ready and the artisan can proceed.",
            )
            sent += 1
        elif (
            payment
            and payment.status == "HELD"
            and booking.status == "ACCEPTED"
            and booking.scheduled_time_start <= window
            and booking.scheduled_time_start > now
        ):
            key = f"conversation:{conversation.id}:service-reminder:{booking.scheduled_time_start.date().isoformat()}"
            record_conversation_event(
                conversation=conversation,
                event_code="SERVICE_REMINDER",
                event_key=key,
                metadata={
                    "booking_id": str(booking.id),
                    "scheduled_start": booking.scheduled_time_start.isoformat(),
                },
            )
            schedule_email(
                event_key=key + ":customer",
                recipient=conversation.customer,
                subject="Your artisan service is coming up",
                message="Your scheduled service is within the next 24 hours. Keep this conversation handy for updates.",
            )
            sent += 1
    return {"status": "ok", "reminders": sent}


@shared_task
def purge_finalized_conversation_images(batch_size=500):
    """Deprecated: photographs are not part of the audio/video completion policy."""
    return {"status": "disabled", "reason": "photos excluded from recording retention"}


@shared_task
def purge_request_recordings(request_id):
    from .retention import purge_request

    return {"request_id": request_id, "status": purge_request(request_id)}


@shared_task
def reconcile_recording_retention():
    from .retention import reconcile
    from .chargebacks import reconcile_chargeback_events

    reconcile_chargeback_events()
    return {"processed": reconcile()}
