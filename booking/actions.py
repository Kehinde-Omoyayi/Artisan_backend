"""Server-derived action availability for a booking.

Nothing in the interface decides on its own whether a button exists: the same
rules that the API endpoints enforce are expressed once here and shipped to the
client as ``available_actions``. A hidden action is also a rejected action, so
the two can never drift apart.
"""

from payments.models import EscrowPayment
from payments.services import COMMITTED_PAYMENT_STATUSES

from .models import Booking

CUSTOMER = "CUSTOMER"
ARTISAN = "ARTISAN"

ACCEPT_BOOKING = "ACCEPT_BOOKING"
REJECT_BOOKING = "REJECT_BOOKING"
CANCEL_BOOKING = "CANCEL_BOOKING"
PAY_BOOKING = "PAY_BOOKING"
START_WORK = "START_WORK"
REQUEST_COMPLETION = "REQUEST_COMPLETION"
CONFIRM_COMPLETION = "CONFIRM_COMPLETION"
DISPUTE_BOOKING = "DISPUTE_BOOKING"
VIEW_HISTORY = "VIEW_HISTORY"
OPEN_CONVERSATION = "OPEN_CONVERSATION"

# Payable means the customer may still produce a valid payment link for it.
PAYABLE_PAYMENT_STATUSES = frozenset(
    {EscrowPayment.Status.PAYMENT_PENDING, EscrowPayment.Status.FAILED}
)


def viewer_role(booking, user):
    if not getattr(user, "is_authenticated", False):
        return None
    if booking.customer.user_id == user.id:
        return CUSTOMER
    if booking.artisan.user_id == user.id:
        return ARTISAN
    return None


def base_payment(booking):
    cached = getattr(booking, "_base_payment", None)
    if cached is not None:
        return cached
    payment = (
        EscrowPayment.objects.filter(booking=booking, addon_order__isnull=True)
        .order_by("created_at")
        .first()
    )
    booking._base_payment = payment
    return payment


def available_action_codes(booking, user):
    role = viewer_role(booking, user)
    if role is None:
        return []
    payment = base_payment(booking)
    status = booking.status
    money_committed = bool(payment and payment.status in COMMITTED_PAYMENT_STATUSES)
    payment_held = bool(payment and payment.status == EscrowPayment.Status.HELD)

    actions = []
    if role == ARTISAN:
        if status == Booking.Status.REQUESTED:
            actions.extend([ACCEPT_BOOKING, REJECT_BOOKING])
        if status == Booking.Status.ACCEPTED and payment_held:
            actions.append(START_WORK)
        if status == Booking.Status.IN_PROGRESS:
            actions.append(REQUEST_COMPLETION)
    else:
        if status in {Booking.Status.REQUESTED, Booking.Status.ACCEPTED} and not money_committed:
            actions.append(CANCEL_BOOKING)
        if status == Booking.Status.ACCEPTED and not money_committed:
            if payment is None or (
                payment.status in PAYABLE_PAYMENT_STATUSES
                and not payment.link_voided_at
            ):
                actions.append(PAY_BOOKING)
        if status == Booking.Status.COMPLETION_REQUESTED:
            actions.append(CONFIRM_COMPLETION)
        if status in {
            Booking.Status.ACCEPTED,
            Booking.Status.IN_PROGRESS,
            Booking.Status.COMPLETION_REQUESTED,
        }:
            actions.append(DISPUTE_BOOKING)
    actions.append(VIEW_HISTORY)
    if getattr(booking, "_conversation_id", None):
        actions.append(OPEN_CONVERSATION)
    return actions


_LABELS = {
    ACCEPT_BOOKING: ("Accept booking", "primary"),
    REJECT_BOOKING: ("Decline booking", "secondary"),
    CANCEL_BOOKING: ("Cancel booking", "danger"),
    PAY_BOOKING: ("Pay for booking", "primary"),
    START_WORK: ("Start work", "primary"),
    REQUEST_COMPLETION: ("Submit completion", "primary"),
    CONFIRM_COMPLETION: ("Confirm completion", "primary"),
    DISPUTE_BOOKING: ("Report a problem", "secondary"),
    VIEW_HISTORY: ("View history", "ghost"),
    OPEN_CONVERSATION: ("Open conversation", "ghost"),
}


def available_actions(booking, user):
    """Ordered list of actions with labels; safe to render directly."""
    result = []
    for code in available_action_codes(booking, user):
        label, variant = _LABELS[code]
        result.append({"code": code, "label": label, "variant": variant})
    return result
