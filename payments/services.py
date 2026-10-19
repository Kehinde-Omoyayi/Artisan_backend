import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from booking.models import Booking
from notifications.events import notify_payment_failed, notify_payment_held

from .calculations import calculate_booking_money
from .flutterwave import FlutterwaveClient, FlutterwaveUnknownError
from .models import EscrowPayment, EscrowRelease
from .ledger import record_financial_event


def record_payment_ledger(*, payment, event_type, event_key, status_before="", status_after="", actor=None, amount_kobo=None, metadata=None):
    return record_financial_event(payment=payment, event_type=event_type, event_key=event_key, status_before=status_before, status_after=status_after, actor=actor, amount_kobo=amount_kobo, metadata=metadata)


class PaymentVerificationPending(Exception):
    """Raised when Flutterwave has not reached a terminal payment state."""


def prepare_booking_payment(*, booking):
    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).select_related("customer__user", "artisan").get(pk=booking.pk)
        if booking.status != Booking.Status.ACCEPTED:
            raise ValueError("Booking must be accepted before payment.")
        payment = EscrowPayment.objects.filter(booking=booking, addon_order__isnull=True).order_by("created_at").first()
        if payment:
            if payment.status in {EscrowPayment.Status.HELD, EscrowPayment.Status.RELEASE_PENDING, EscrowPayment.Status.RELEASED}:
                return payment
            if payment.status == EscrowPayment.Status.DISPUTED:
                raise ValueError("This payment is disputed.")
            if payment.status == EscrowPayment.Status.REFUNDED:
                raise ValueError("This payment has already been refunded.")
            if payment.status == EscrowPayment.Status.FAILED:
                old_status = payment.status
                payment.status = EscrowPayment.Status.PAYMENT_PENDING
                # A fresh provider reference per attempt: a late callback or webhook for the failed attempt can
                # then never be matched to (and fail) the new attempt.
                payment.reference = f"PAY-{booking.id}-{uuid.uuid4().hex[:16]}"
                payment.failure_reason = ""
                payment.provider_transaction_id = None
                payment.provider_reference = ""
                payment.provider_status = ""
                payment.provider_payload = {}
                payment.save(update_fields=["status", "reference", "failure_reason", "provider_transaction_id", "provider_reference", "provider_status", "provider_payload", "updated_at"])
                record_payment_ledger(payment=payment, event_type="PAYMENT_RESET", event_key=f"payment:{payment.id}:reset:{payment.updated_at.timestamp()}", status_before=old_status, status_after=payment.status)
                return payment
            return payment
        service_amount_kobo = int((booking.price_agreed * Decimal("100")).quantize(Decimal("1")))
        if service_amount_kobo <= 0 or service_amount_kobo % 100:
            raise ValueError("Booking price must be a positive whole NGN amount for the current Flutterwave integration.")
        amounts = calculate_booking_money(service_amount_kobo)
        return EscrowPayment.objects.create(
            booking=booking, customer=booking.customer.user, artisan=booking.artisan,
            reference=f"PAY-{booking.id}-{uuid.uuid4().hex[:16]}",
            service_amount_kobo=amounts["service_amount_kobo"], customer_fee_kobo=amounts["customer_fee_kobo"],
            total_amount_kobo=amounts["total_amount_kobo"], commission_rate=amounts["commission_rate"],
            platform_commission_kobo=amounts["platform_commission_kobo"], artisan_payout_kobo=amounts["artisan_payout_kobo"],
            status=EscrowPayment.Status.PAYMENT_PENDING,
        )

def prepare_additional_order_payment(*, order, user):
    """Prepare the customer's independent escrow payment for one approved add-on order."""
    from booking.models import BookingAdditionalOffer, BookingAdditionalOrder

    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).select_related("customer__user", "artisan").get(pk=order.booking_id)
        locked_order = BookingAdditionalOrder.objects.select_for_update(of=("self",)).select_related("accepted_offer").get(pk=order.pk, booking=booking)
        if booking.customer.user_id != user.id:
            raise PermissionDenied("Only the booking customer can pay for an add-on order.")
        if booking.status != Booking.Status.IN_PROGRESS:
            raise ValueError("Add-on payment is only available while the parent service is in progress.")
        if locked_order.status != BookingAdditionalOrder.Status.APPROVED or not locked_order.accepted_offer_id:
            raise ValueError("The add-on order must be approved before payment.")
        if locked_order.accepted_offer.status != BookingAdditionalOffer.Status.ACCEPTED:
            raise ValueError("The accepted add-on offer is not in a payable state.")

        payment = EscrowPayment.objects.select_for_update(of=("self",)).filter(addon_order=locked_order).first()
        if payment:
            if payment.status in {EscrowPayment.Status.HELD, EscrowPayment.Status.RELEASE_PENDING, EscrowPayment.Status.RELEASED}:
                return payment
            if payment.status in {EscrowPayment.Status.DISPUTED, EscrowPayment.Status.REFUNDED}:
                raise ValueError("This add-on payment is disputed or has already been refunded.")
            if payment.status == EscrowPayment.Status.FAILED:
                old_status = payment.status
                payment.status = EscrowPayment.Status.PAYMENT_PENDING
                payment.reference = f"PAY-ADDON-{locked_order.id}-{uuid.uuid4().hex[:16]}"
                payment.failure_reason = ""
                payment.provider_transaction_id = None
                payment.provider_reference = ""
                payment.provider_status = ""
                payment.provider_payload = {}
                payment.verification_payload = {}
                payment.verified_at = None
                payment.held_at = None
                payment.save(update_fields=["status", "reference", "failure_reason", "provider_transaction_id", "provider_reference", "provider_status", "provider_payload", "verification_payload", "verified_at", "held_at", "updated_at"])
                record_payment_ledger(payment=payment, event_type="PAYMENT_RESET", event_key=f"payment:{payment.id}:reset:{payment.updated_at.timestamp()}", status_before=old_status, status_after=payment.status)
                return payment
            return payment

        service_amount_kobo = int((locked_order.accepted_offer.amount * Decimal("100")).quantize(Decimal("1")))
        if service_amount_kobo <= 0 or service_amount_kobo % 100:
            raise ValueError("Add-on amount must be a positive whole NGN amount for the current Flutterwave integration.")
        amounts = calculate_booking_money(service_amount_kobo)
        return EscrowPayment.objects.create(
            booking=booking,
            addon_order=locked_order,
            customer=booking.customer.user,
            artisan=booking.artisan,
            reference=f"PAY-ADDON-{locked_order.id}-{uuid.uuid4().hex[:16]}",
            service_amount_kobo=amounts["service_amount_kobo"],
            customer_fee_kobo=amounts["customer_fee_kobo"],
            total_amount_kobo=amounts["total_amount_kobo"],
            commission_rate=amounts["commission_rate"],
            platform_commission_kobo=amounts["platform_commission_kobo"],
            artisan_payout_kobo=amounts["artisan_payout_kobo"],
            status=EscrowPayment.Status.PAYMENT_PENDING,
        )

def _initialize_prepared_payment_external(payment):
    customer = payment.customer
    result = FlutterwaveClient().initialize_payment(
        reference=payment.reference,
        amount_ngn=payment.total_amount_kobo // 100,
        customer=customer,
        booking_id=payment.booking_id,
        addon_order_id=payment.addon_order_id,
    )
    provider_data = result.get("data") or {}
    with transaction.atomic():
        locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
        locked.provider_transaction_id = str(provider_data.get("id")) if provider_data.get("id") else locked.provider_transaction_id
        locked.provider_reference = locked.reference
        locked.provider_payload = result
        locked.provider_status = str(result.get("status") or "initialized")[:50]
        locked.save(update_fields=["provider_transaction_id", "provider_reference", "provider_payload", "provider_status", "updated_at"])
        return locked, result

def initialize_booking_payment(*, booking):
    payment = prepare_booking_payment(booking=booking)
    if payment.status == EscrowPayment.Status.HELD:
        return payment, None
    if payment.provider_transaction_id:
        return payment, payment.provider_payload
    try:
        return _initialize_prepared_payment_external(payment)
    except FlutterwaveUnknownError:
        raise ValueError("Payment initialization outcome is unknown. Verify the transaction before retrying.") from None

def verify_and_hold_payment(payment_id, provider_transaction_id=None):
    payment_snapshot = EscrowPayment.objects.only("booking_id", "addon_order_id").get(pk=payment_id)
    with transaction.atomic():
        # Lock the parent first, matching booking completion and add-on negotiation lock ordering.
        Booking.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.booking_id)
        if payment_snapshot.addon_order_id:
            from booking.models import BookingAdditionalOrder
            BookingAdditionalOrder.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.addon_order_id)
        payment = EscrowPayment.objects.select_for_update(of=("self",)).select_related("booking", "artisan", "addon_order").get(pk=payment_id)
        if payment.status in {EscrowPayment.Status.HELD, EscrowPayment.Status.DISPUTED}:
            return payment
        if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
            raise ValueError("Payment is not awaiting verification.")
        if not payment.provider_transaction_id and not provider_transaction_id:
            raise ValueError("Provider transaction ID is missing.")
        provider_transaction_id = payment.provider_transaction_id or str(provider_transaction_id)
        expected_reference = payment.reference
        expected_amount = payment.total_amount_kobo // 100

    result = FlutterwaveClient().verify_transaction(provider_transaction_id)
    data = result.get("data") or {}
    provider_status = str(data.get("status", "")).lower()
    provider_reference = str(data.get("tx_ref", "")).strip()
    provider_currency = str(data.get("currency", "")).upper()
    provider_amount = Decimal(str(data.get("amount", "0")))
    provider_id = str(data.get("id", ""))

    if provider_status not in {"successful", "failed", "cancelled"}:
        raise PaymentVerificationPending("Flutterwave has not confirmed this payment.")

    identity_mismatch = (
        provider_id != str(provider_transaction_id)
        or provider_reference != expected_reference
        or provider_currency != "NGN"
        or provider_amount != Decimal(expected_amount)
    )

    if identity_mismatch:
        raise ValueError("Flutterwave transaction does not match the expected payment.")

    failure_reason = "" if provider_status == "successful" else "Flutterwave did not report a successful payment."

    if failure_reason:
        with transaction.atomic():
            Booking.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.booking_id)
            if payment_snapshot.addon_order_id:
                from booking.models import BookingAdditionalOrder
                BookingAdditionalOrder.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.addon_order_id)
            payment = EscrowPayment.objects.select_for_update(of=("self",)).select_related("booking", "artisan", "addon_order").get(pk=payment_id)
            if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
                return payment
            payment.status = EscrowPayment.Status.FAILED
            payment.provider_status = provider_status
            payment.verification_payload = result
            payment.failure_reason = failure_reason
            payment.save(update_fields=["status", "provider_status", "verification_payload", "failure_reason", "updated_at"])
            transaction.on_commit(lambda: notify_payment_failed(payment))
            from conversations.events import record_booking_event
            transaction.on_commit(lambda payment=payment: record_booking_event(booking=payment.booking, event_code="PAYMENT_FAILED", event_key=f"payment:{payment.id}:failed", metadata={"payment_id": str(payment.id)}))
        return payment

    with transaction.atomic():
        Booking.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.booking_id)
        addon_order = None
        if payment_snapshot.addon_order_id:
            from booking.models import BookingAdditionalOrder
            addon_order = BookingAdditionalOrder.objects.select_for_update(of=("self",)).select_related("accepted_offer").get(pk=payment_snapshot.addon_order_id)
        payment = EscrowPayment.objects.select_for_update(of=("self",)).select_related("booking", "artisan", "addon_order").get(pk=payment_id)
        if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
            return payment
        # Provider-confirmed money is always recorded. An add-on is payable while its parent service is in
        # progress; the original booking payment is payable before the service starts. Disputed payments stay
        # frozen even when provider verification arrives after the dispute was opened.
        expected_booking_status = Booking.Status.IN_PROGRESS if payment.addon_order_id else Booking.Status.ACCEPTED
        orphaned = payment.booking.status != expected_booking_status
        if payment.addon_order_id and (addon_order is None or addon_order.status != "APPROVED"):
            orphaned = True
        disputed = payment.booking.status == Booking.Status.DISPUTED
        now = timezone.now()
        old_status = payment.status
        payment.status = EscrowPayment.Status.DISPUTED if disputed else EscrowPayment.Status.HELD
        payment.provider_status = provider_status
        payment.verification_payload = result
        payment.verified_at = now
        payment.held_at = now
        if disputed:
            payment.failure_reason = "Provider confirmed this payment while the booking was disputed. Funds remain frozen for support review."
        else:
            payment.failure_reason = "Provider confirmed this payment after the booking stopped being payable. Refund review required." if orphaned else ""
        payment.save(update_fields=["status", "provider_status", "verification_payload", "verified_at", "held_at", "failure_reason", "updated_at"])
        record_payment_ledger(payment=payment, event_type="ESCROW_HELD", event_key=f"payment:{payment.id}:held", status_before=old_status, status_after=payment.status, amount_kobo=payment.total_amount_kobo)
        transaction.on_commit(lambda: notify_payment_held(payment))
        if orphaned:
            from notifications.events import notify_support
            transaction.on_commit(lambda payment=payment: notify_support(event_key=f"payment-orphaned:{payment.id}", subject="Paid booking is no longer payable", message=f"Payment {payment.reference} was confirmed by the provider while booking {payment.booking_id} was {payment.booking.status}. The funds are held and need a refund review."), robust=True)
        from conversations.events import record_booking_event
        transaction.on_commit(lambda payment=payment: record_booking_event(booking=payment.booking, event_code="PAYMENT_CONFIRMED", event_key=f"payment:{payment.id}:confirmed", metadata={"payment_id": str(payment.id)}))
        payment_event = "PAYMENT_DISPUTED" if disputed else "PAYMENT_HELD"
        transaction.on_commit(lambda payment=payment, payment_event=payment_event: record_booking_event(booking=payment.booking, event_code=payment_event, event_key=f"payment:{payment.id}:{payment_event.lower()}", metadata={"payment_id": str(payment.id)}))
    return payment

def mark_payment_release_pending(payment_id):
    payment_snapshot = EscrowPayment.objects.only("booking_id", "addon_order_id").get(pk=payment_id)
    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.booking_id)
        if payment_snapshot.addon_order_id:
            from booking.models import BookingAdditionalOrder
            BookingAdditionalOrder.objects.select_for_update(of=("self",)).get(pk=payment_snapshot.addon_order_id)
        payment = EscrowPayment.objects.select_for_update(of=("self",)).select_related("booking", "addon_order").get(pk=payment_id)
        booking = payment.booking

        if payment.status == EscrowPayment.Status.RELEASED:
            return payment.release
        if payment.status != EscrowPayment.Status.HELD:
            raise ValueError("Payment must be held before payout release.")
        if booking.status != Booking.Status.COMPLETED:
            raise ValueError("Booking must be completed before payout release.")
        if not booking.artisan_completed_at or not booking.customer_completed_at:
            raise ValueError("Both customer and artisan must complete the booking.")

        if not booking.payout_eligible_at:
            booking.payout_eligible_at = timezone.now()
            booking.save(update_fields=["payout_eligible_at", "updated_at"])

        old_status = payment.status
        payment.status = EscrowPayment.Status.RELEASE_PENDING
        payment.release_requested_at = timezone.now()
        payment.save(update_fields=["status", "release_requested_at", "updated_at"])
        record_payment_ledger(payment=payment, event_type="RELEASE_PENDING", event_key=f"payment:{payment.id}:release-pending", status_before=old_status, status_after=payment.status, amount_kobo=payment.artisan_payout_kobo)

        # Preserve the established base-payment reference; each independent add-on needs its own unique
        # provider payout reference so separate payment releases cannot collide.
        release_reference = f"RELEASE-{booking.id}" if payment.addon_order_id is None else f"RELEASE-ADDON-{payment.addon_order_id}"
        release, _ = EscrowRelease.objects.get_or_create(
            payment=payment,
            defaults={
                "reference": release_reference,
                "amount_kobo": payment.artisan_payout_kobo,
                "status": EscrowRelease.Status.PENDING,
            },
        )
        return release
