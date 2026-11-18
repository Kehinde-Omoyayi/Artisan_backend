from django.utils import timezone

from booking.models import Booking
from core.state import BOOKING_TERMINAL_STATUSES
from .models import Offer

CONVERSATION_STATES = (
    "OPEN",
    "NEGOTIATING",
    "BOOKING",
    "ACTIVE",
    "COMPLETED",
    "DISPUTED",
    "CLOSED",
)


def derive_conversation_state(*, conversation, booking=None, offer=None, payment=None):
    if (
        conversation.service_request_id
        and conversation.service_request.status == "CLOSED"
    ):
        return "CLOSED"
    # A cancelled or declined booking is closed history, not the current state:
    # once the artisan (re)sends a quote the room is negotiating again.
    live_offer = (
        offer is not None
        and offer.status
        in {Offer.Status.PROPOSED, Offer.Status.REVISION_REQUESTED}
        and offer.expires_at > timezone.now()
    )
    renegotiable = (
        live_offer
        and booking is not None
        and booking.status
        in {Booking.Status.CANCELLED, Booking.Status.REJECTED}
    )
    if booking is not None and not renegotiable:
        payment_status = getattr(payment, "status", None)
        if booking.status == Booking.Status.DISPUTED or payment_status == "DISPUTED":
            return "DISPUTED"
        if booking.status == Booking.Status.COMPLETED:
            return "COMPLETED"
        if booking.status in BOOKING_TERMINAL_STATUSES:
            return "CLOSED"
        if (
            booking.status
            in {Booking.Status.IN_PROGRESS, Booking.Status.COMPLETION_REQUESTED}
            or payment_status == "HELD"
        ):
            return "ACTIVE"
        if booking.status in {
            Booking.Status.REQUESTED,
            Booking.Status.ACCEPTED,
        } or payment_status in {"PAYMENT_PENDING", "RELEASE_PENDING", "REFUND_PENDING"}:
            return "BOOKING"
    if offer is not None and offer.status in {
        Offer.Status.PROPOSED,
        Offer.Status.REVISION_REQUESTED,
    }:
        return "NEGOTIATING"
    return "OPEN"
