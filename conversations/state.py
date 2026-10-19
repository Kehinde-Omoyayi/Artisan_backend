from booking.models import Booking
from core.state import BOOKING_TERMINAL_STATUSES
from .models import Offer

CONVERSATION_STATES = ("OPEN", "NEGOTIATING", "BOOKING", "ACTIVE", "COMPLETED", "DISPUTED", "CLOSED")


def derive_conversation_state(*, conversation, booking=None, offer=None, payment=None):
    if booking is not None:
        payment_status = getattr(payment, "status", None)
        if booking.status == Booking.Status.DISPUTED or payment_status == "DISPUTED": return "DISPUTED"
        if booking.status == Booking.Status.COMPLETED: return "COMPLETED"
        if booking.status in BOOKING_TERMINAL_STATUSES: return "CLOSED"
        if booking.status in {Booking.Status.IN_PROGRESS, Booking.Status.COMPLETION_REQUESTED} or payment_status == "HELD": return "ACTIVE"
        if booking.status in {Booking.Status.REQUESTED, Booking.Status.ACCEPTED} or payment_status in {"PAYMENT_PENDING", "RELEASE_PENDING", "REFUND_PENDING"}: return "BOOKING"
    if offer is not None and offer.status == Offer.Status.PROPOSED: return "NEGOTIATING"
    return "OPEN"
