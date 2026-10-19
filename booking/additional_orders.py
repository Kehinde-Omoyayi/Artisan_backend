from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.policies import MAX_SERVICE_AMOUNT_KOBO
from payments.models import EscrowPayment
from .models import Booking, BookingAdditionalOffer, BookingAdditionalOrder


def _normalize_addon_amount(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"amount": "Enter a valid add-on order amount."}) from exc
    if not amount.is_finite() or amount <= 0:
        raise ValidationError({"amount": "Add-on order amount must be greater than zero."})
    amount_kobo = int(amount * 100)
    if amount_kobo % 100:
        raise ValidationError({"amount": "Add-on payments must be in whole naira for the current payment provider."})
    maximum = getattr(settings, "MAX_SERVICE_AMOUNT_KOBO", MAX_SERVICE_AMOUNT_KOBO)
    if amount_kobo > maximum:
        raise ValidationError({"amount": "Add-on order amount exceeds the platform transaction limit."})
    return amount


def _participant_role(booking, user):
    if booking.customer.user_id == user.id:
        return "customer"
    if booking.artisan.user_id == user.id:
        return "artisan"
    raise PermissionDenied("Only the customer or assigned artisan can manage add-on orders.")


def _notify_addon_order(*, order, recipient, event_key, subject, message):
    if not recipient:
        return
    from notifications.events import schedule_email
    from notifications.links import booking_link, conversation_link
    from conversations.models import Conversation

    conversation = Conversation.objects.filter(service_request_id=order.booking.request_id).first()
    schedule_email(
        event_key=f"additional-order:{order.id}:{event_key}:{recipient.id}",
        recipient=recipient,
        subject=subject,
        message=message,
        action_url=conversation_link(conversation) if conversation else booking_link(order.booking),
    )


def _record_addon_event(*, order, offer, event_code, body):
    from conversations.events import record_booking_event

    return record_booking_event(
        booking=order.booking,
        event_code=event_code,
        event_key=f"additional-order:{order.id}:offer:{offer.id}:{event_code.lower()}",
        metadata={
            "additional_order_id": str(order.id),
            "offer_id": str(offer.id),
            "revision": offer.revision,
            "amount": str(offer.amount),
            "order_status": order.status,
        },
        body=body,
    )


def _assert_addon_payment_ready(booking):
    base_payment = EscrowPayment.objects.select_for_update(of=("self",)).filter(
        booking=booking, addon_order__isnull=True
    ).first()
    if base_payment is None or base_payment.status != EscrowPayment.Status.HELD:
        raise ValidationError("The original booking payment must be held in escrow before adding another order.")


def create_additional_order(*, booking_id, user, amount, description):
    """Create an independent add-on order without modifying the parent booking's original scope or price."""
    amount = _normalize_addon_amount(amount)
    description = str(description or "").strip()
    if not description or len(description) > 3000:
        raise ValidationError({"description": "Describe the separate add-on work in 1 to 3,000 characters."})

    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update(of=("self",)).select_related(
                "customer__user", "artisan__user", "request"
            ).get(pk=booking_id)
        except Booking.DoesNotExist as exc:
            raise ValidationError("Booking not found.") from exc
        role = _participant_role(booking, user)
        if role != "artisan":
            raise PermissionDenied("Only the assigned artisan can create a separate add-on order.")
        if booking.status != Booking.Status.IN_PROGRESS:
            raise ValidationError("Additional orders can only be proposed while the service is in progress.")
        _assert_addon_payment_ready(booking)

        order = BookingAdditionalOrder.objects.create(
            booking=booking,
            created_by=user,
            status=BookingAdditionalOrder.Status.NEGOTIATING,
        )
        offer = BookingAdditionalOffer.objects.create(
            order=order,
            revision=1,
            created_by=user,
            amount=amount,
            description=description,
            status=BookingAdditionalOffer.Status.PROPOSED,
        )
        from conversations.events import record_booking_event
        record_booking_event(
            booking=booking,
            event_code="ADDITIONAL_ORDER_PROPOSED",
            event_key=f"additional-order:{order.id}:offer:{offer.id}:proposed",
            metadata={"additional_order_id": str(order.id), "offer_id": str(offer.id), "amount": str(amount)},
            body=f"The artisan proposed a separate add-on order for NGN {amount:,.2f}. Original booking price is unchanged. Scope: {description}",
        )
        booking.save(update_fields=["updated_at"])
        _notify_addon_order(
            order=order,
            recipient=booking.customer.user,
            event_key=f"offer-{offer.id}-proposed",
            subject="A separate add-on order needs your review",
            message=f"The artisan proposed a separate add-on order for NGN {amount:,.2f}. It does not replace the original booking. Review the scope and accept, decline, or counter it in the conversation.",
        )
    return order


def respond_to_additional_order(*, booking_id, order_id, offer_id, user, action, amount=None, description=None):
    if action not in {"accept", "decline", "counter"}:
        raise ValueError("Unsupported additional-order response.")
    counter_amount = _normalize_addon_amount(amount) if action == "counter" else None
    counter_description = str(description or "").strip() if action == "counter" else ""
    if action == "counter" and (not counter_description or len(counter_description) > 3000):
        raise ValidationError({"description": "Describe the counter-proposal in 1 to 3,000 characters."})

    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update(of=("self",)).select_related(
                "customer__user", "artisan__user", "request"
            ).get(pk=booking_id)
            order = BookingAdditionalOrder.objects.select_for_update(of=("self",)).get(
                pk=order_id, booking=booking
            )
            offer = BookingAdditionalOffer.objects.select_for_update(of=("self",)).get(
                pk=offer_id, order=order
            )
        except (Booking.DoesNotExist, BookingAdditionalOrder.DoesNotExist, BookingAdditionalOffer.DoesNotExist) as exc:
            raise ValidationError("Add-on order or offer not found for this booking.") from exc

        _participant_role(booking, user)
        if booking.status != Booking.Status.IN_PROGRESS:
            raise ValidationError("Add-on orders can only be answered while the service is in progress.")
        if order.status != BookingAdditionalOrder.Status.NEGOTIATING:
            raise ValidationError("This add-on order is no longer open for negotiation.")
        if offer.status != BookingAdditionalOffer.Status.PROPOSED:
            raise ValidationError("Only the current proposed add-on offer can be answered.")
        if offer.created_by_id == user.id:
            raise PermissionDenied("You cannot respond to your own add-on offer.")
        latest_id = order.offers.order_by("-revision").values_list("id", flat=True).first()
        if latest_id != offer.id:
            raise ValidationError("Only the latest add-on offer can be answered.")

        other_party = offer.created_by
        offer.responded_at = timezone.now()
        if action == "accept":
            offer.status = BookingAdditionalOffer.Status.ACCEPTED
            offer.accepted_by = user
            offer.save(update_fields=["status", "accepted_by", "responded_at", "updated_at"])
            order.status = BookingAdditionalOrder.Status.APPROVED
            order.accepted_offer = offer
            order.save(update_fields=["status", "accepted_offer", "updated_at"])
            event_code = "ADDITIONAL_ORDER_ACCEPTED"
            body = f"The separate add-on order was approved for NGN {offer.amount:,.2f}. The customer must pay it through marketplace escrow before service completion."
            subject = "Your add-on order was approved"
            message = f"The separate add-on order for NGN {offer.amount:,.2f} was approved. The customer can now pay securely through escrow."
            recipient_event = "accepted"
        elif action == "decline":
            offer.status = BookingAdditionalOffer.Status.DECLINED
            offer.save(update_fields=["status", "responded_at", "updated_at"])
            order.status = BookingAdditionalOrder.Status.DECLINED
            order.save(update_fields=["status", "updated_at"])
            event_code = "ADDITIONAL_ORDER_DECLINED"
            body = f"The separate add-on order for NGN {offer.amount:,.2f} was declined. No extra payment was collected."
            subject = "Your add-on order was declined"
            message = f"The separate add-on order for NGN {offer.amount:,.2f} was declined. No extra payment was collected."
            recipient_event = "declined"
        else:
            offer.status = BookingAdditionalOffer.Status.COUNTERED
            offer.save(update_fields=["status", "responded_at", "updated_at"])
            next_offer = BookingAdditionalOffer.objects.create(
                order=order,
                parent=offer,
                revision=offer.revision + 1,
                created_by=user,
                amount=counter_amount,
                description=counter_description,
                status=BookingAdditionalOffer.Status.PROPOSED,
            )
            event_code = "ADDITIONAL_ORDER_COUNTERED"
            body = f"A counter-offer was made on the separate add-on order for NGN {counter_amount:,.2f}. Scope: {counter_description}"
            subject = "A counter-offer was sent on an add-on order"
            message = f"A counter-offer for NGN {counter_amount:,.2f} was sent on the separate add-on order. Review and respond in the service conversation."
            recipient_event = f"countered-{next_offer.id}"
            offer = next_offer

        _record_addon_event(order=order, offer=offer, event_code=event_code, body=body)
        booking.save(update_fields=["updated_at"])
        _notify_addon_order(
            order=order,
            recipient=other_party,
            event_key=f"{recipient_event}-offer-{offer.id}",
            subject=subject,
            message=message,
        )
    return order, offer


def approved_addon_funding(booking, *, lock=False):
    """Return (all_funded, funded_payments, unpaid_orders) for the booking's approved add-ons."""
    order_qs = BookingAdditionalOrder.objects.filter(
        booking=booking, status=BookingAdditionalOrder.Status.APPROVED
    ).order_by("pk")
    if lock:
        order_qs = order_qs.select_for_update(of=("self",))
    orders = list(order_qs)
    if not orders:
        return True, [], []

    payment_qs = EscrowPayment.objects.filter(addon_order_id__in=[order.pk for order in orders]).order_by("addon_order_id")
    if lock:
        payment_qs = payment_qs.select_for_update(of=("self",))
    payments_by_order = {payment.addon_order_id: payment for payment in payment_qs}
    funded_payments = []
    unpaid_orders = []
    for order in orders:
        payment = payments_by_order.get(order.pk)
        if payment is None or payment.status != EscrowPayment.Status.HELD:
            unpaid_orders.append(order)
        else:
            funded_payments.append(payment)
    return not unpaid_orders, funded_payments, unpaid_orders
