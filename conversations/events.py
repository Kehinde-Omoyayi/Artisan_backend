import logging
from decimal import Decimal

from django.db import IntegrityError, transaction

from .models import Conversation, Message

logger = logging.getLogger(__name__)

_EVENT_TEXT = {
    "CONVERSATION_STARTED": "Conversation started.",
    "REQUEST_COMPLETED": "The service request details are complete and ready for an offer.",
    "OFFER_ACCEPTED": "Offer accepted.",
    "OFFER_DECLINED": "Offer declined.",
    "OFFER_EXPIRED": "Offer expired.",
    "BOOKING_CREATED": "Booking created.",
    "BOOKING_ACCEPTED": "Booking accepted.",
    "PAYMENT_PENDING": "Payment is pending.",
    "PAYMENT_INITIALIZED": "Payment initialized. Checkout is ready.",
    "PAYMENT_FAILED": "Payment could not be confirmed.",
    "PAYMENT_CONFIRMED": "Payment confirmed.",
    "PAYMENT_HELD": "Payment is held in escrow.",
    "PAYMENT_DISPUTED": "Payment is protected while the dispute is reviewed.",
    "RELEASE_PENDING": "Escrow release is pending payout processing.",
    "SERVICE_STARTED": "SERVICE IN PROGRESS.",
    "SERVICE_PROGRESS_UPDATED": "Service progress updated.",
    "COMPLETION_REQUESTED": "SERVICE COMPLETION REQUEST.",
    "COMPLETION_ACCEPTED": "Service completion accepted.",
    "DISPUTE_OPENED": "DISPUTE OPENED.",
    "SUPPORT_CASE_CREATED": "A support case was created for this booking.",
    "SUPPORT_CASE_UPDATED": "The support case was updated.",
    "SUPPORT_CASE_RESOLVED": "The support case was resolved.",
    "SUPPORT_CASE_CLOSED": "The support case was closed.",
    "REFUND_PENDING": "Refund processing is pending.",
    "REFUND_FAILED": "The refund failed and requires review.",
    "REFUNDED": "Refund completed.",
    "PARTIAL_REFUND_COMPLETED": "Partial refund completed.",
    "PAYOUT_INITIATED": "Artisan payout processing has started.",
    "PAYOUT_COMPLETED": "Artisan payout completed.",
    "PAYOUT_FAILED": "Artisan payout failed and requires review.",
    "REVIEW_SUBMITTED": "Customer review submitted.",
    "ADDITIONAL_ORDER_PROPOSED": "A separate add-on order was proposed. The original booking price and scope are unchanged.",
    "ADDITIONAL_ORDER_COUNTERED": "A counter-offer was made on a separate add-on order.",
    "ADDITIONAL_ORDER_ACCEPTED": "A separate add-on order was approved and is ready for escrow payment.",
    "ADDITIONAL_ORDER_DECLINED": "A separate add-on order was declined. No extra payment was collected.",
    "ADDITIONAL_ORDER_PAYMENT_HELD": "Payment for a separate add-on order is held in escrow.",
}


def record_conversation_event(*, conversation, event_code, event_key, metadata=None, body=None):
    metadata = metadata or {}
    try:
        with transaction.atomic():
            message, _ = Message.objects.get_or_create(
                event_key=event_key,
                defaults={
                    "conversation": conversation,
                    "sender": None,
                    "message_type": Message.MessageType.SYSTEM,
                    "event_code": event_code,
                    "body": body or _EVENT_TEXT.get(event_code, "Marketplace event recorded."),
                    "metadata": metadata,
                },
            )
    except IntegrityError:
        return Message.objects.filter(event_key=event_key, conversation=conversation).first()
    if message.conversation_id != conversation.id:
        logger.error("Conversation event collision: event_key=%s", event_key); return None
    try:
        transaction.on_commit(lambda message=message: __import__("conversations.realtime", fromlist=["publish_event"]).publish_event(conversation_id=conversation.id, payload={"event":"conversation.updated","message_id":str(message.id),"event_code":message.event_code}))
    except Exception:
        logger.exception("conversation_realtime_schedule_failed conversation=%s", conversation.id)
    return message


def record_booking_event(*, booking, event_code, event_key=None, metadata=None, body=None):
    conversation = Conversation.objects.filter(
        service_request_id=booking.request_id,
        conversation_type=Conversation.ConversationType.SERVICE,
        customer_id=booking.customer.user_id,
        artisan_id=booking.artisan_id,
        service_request__requester_id=booking.customer.user_id,
    ).first()
    if not conversation:
        return None
    return record_conversation_event(
        conversation=conversation,
        event_code=event_code,
        event_key=event_key or f"booking:{booking.id}:{event_code.lower()}",
        metadata=metadata,
        body=body,
    )


def money_text(amount_kobo):
    return f"NGN {Decimal(amount_kobo) / Decimal('100'):,.2f}"
