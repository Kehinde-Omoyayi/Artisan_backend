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
    now = timezone.now(); window = now + timedelta(hours=24); sent = 0
    conversations = Conversation.objects.select_related("customer", "artisan__user").filter(updated_at__gte=now - timedelta(days=30))[:1000]
    for conversation in conversations:
        booking = _booking_for_conversation(conversation)
        if not booking: continue
        payment = getattr(booking, "escrow_payment", None)
        if payment and payment.status == "PAYMENT_PENDING" and booking.status == "ACCEPTED" and booking.updated_at <= now - timedelta(hours=1):
            key = f"conversation:{conversation.id}:payment-reminder:{now.date().isoformat()}"; record_conversation_event(conversation=conversation, event_code="PAYMENT_REMINDER", event_key=key, metadata={"booking_id": str(booking.id)})
            schedule_email(event_key=key+":customer", recipient=conversation.customer, subject="Your booking is waiting for payment", message="Your booking is ready. Complete payment when you are ready and the artisan can proceed."); sent += 1
        elif payment and payment.status == "HELD" and booking.status == "ACCEPTED" and booking.scheduled_time_start <= window and booking.scheduled_time_start > now:
            key = f"conversation:{conversation.id}:service-reminder:{booking.scheduled_time_start.date().isoformat()}"; record_conversation_event(conversation=conversation, event_code="SERVICE_REMINDER", event_key=key, metadata={"booking_id": str(booking.id), "scheduled_start": booking.scheduled_time_start.isoformat()}); schedule_email(event_key=key+":customer", recipient=conversation.customer, subject="Your artisan service is coming up", message="Your scheduled service is within the next 24 hours. Keep this conversation handy for updates."); sent += 1
    return {"status": "ok", "reminders": sent}


@shared_task

def purge_finalized_conversation_images(batch_size=500):
    """Delete only old service-chat images after both service and payment finalization.

    Text, bookings, payments, direct chats, profile/catalogue media, and active
    disputes are deliberately outside this retention task. There is currently no
    conversation-thumbnail model or generated thumbnail storage to clean.
    """
    from datetime import timedelta

    from django.conf import settings
    from django.db import transaction
    from django.db.models import Exists, OuterRef
    from django.utils import timezone

    from booking.models import Booking
    from payments.models import EscrowPayment
    from support.models import SupportCase
    from .models import ConversationAttachment

    retention_days = int(getattr(settings, "CONVERSATION_IMAGE_RETENTION_DAYS", 7) or 0)
    if retention_days < 1:
        return {"status": "disabled", "deleted": 0, "failed": 0}
    batch_size = max(1, min(int(batch_size or 500), 5000))
    now = timezone.now()
    cutoff = now - timedelta(days=retention_days)

    open_disputes = SupportCase.objects.filter(
        booking_id=OuterRef("pk"),
        case_type=SupportCase.CaseType.DISPUTE,
        status__in=(SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW),
    )
    unsettled_payments = EscrowPayment.objects.filter(
        booking_id=OuterRef("pk"),
        status__in=(
            EscrowPayment.Status.PAYMENT_PENDING,
            EscrowPayment.Status.HELD,
            EscrowPayment.Status.DISPUTED,
            EscrowPayment.Status.RELEASE_PENDING,
            EscrowPayment.Status.REFUND_PENDING,
        ),
    )
    active_bookings_for_request = Booking.objects.filter(
        request_id=OuterRef("request_id"),
        status__in=(
            Booking.Status.REQUESTED,
            Booking.Status.ACCEPTED,
            Booking.Status.IN_PROGRESS,
            Booking.Status.COMPLETION_REQUESTED,
        ),
    )
    finalized_bookings = (
        Booking.objects.filter(
            finalized_at__isnull=False,
            finalized_at__lte=cutoff,
            status__in=(
                Booking.Status.COMPLETED,
                Booking.Status.CANCELLED,
                Booking.Status.REJECTED,
                Booking.Status.DISPUTED,
            ),
        )
        .annotate(
            has_open_dispute=Exists(open_disputes),
            has_unsettled_payment=Exists(unsettled_payments),
            has_active_booking=Exists(active_bookings_for_request),
        )
        .filter(
            has_open_dispute=False,
            has_unsettled_payment=False,
            has_active_booking=False,
        )
        .order_by()
    )

    def eligible_attachments():
        matching_bookings = finalized_bookings.filter(
            artisan_id=OuterRef("message__conversation__artisan_id"),
            customer__user_id=OuterRef("message__conversation__customer_id"),
            request__requester_id=OuterRef("message__conversation__customer_id"),
        )
        return ConversationAttachment.objects.filter(
            content_type__istartswith="image/",
            uploaded_at__lte=cutoff,
            message__conversation__conversation_type="SERVICE",
            message__conversation__customer_id__isnull=False,
            message__conversation__artisan_id__isnull=False,
            message__conversation__service_request_id__in=matching_bookings.values("request_id"),
        )

    attachment_ids = list(
        eligible_attachments().order_by("uploaded_at").values_list("pk", flat=True)[:batch_size]
    )
    deleted = failed = 0
    for attachment_id in attachment_ids:
        try:
            with transaction.atomic():
                attachment = (
                    eligible_attachments()
                    .select_for_update()
                    .select_related("message__conversation")
                    .filter(pk=attachment_id)
                    .first()
                )
                if attachment is None:
                    continue
                storage = attachment.file.storage
                name = attachment.file.name
                # Delete first so a storage failure leaves the database record for a
                # later retry. The model's post-delete hook repeats this idempotently
                # after commit; missing files are harmless in standard storage backends.
                if storage and name:
                    storage.delete(name)
                attachment.delete()
                deleted += 1
        except Exception:
            failed += 1
            logger.exception("conversation_image_retention_delete_failed attachment_id=%s", attachment_id)

    return {"status": "ok", "deleted": deleted, "failed": failed, "cutoff": cutoff.isoformat()}
