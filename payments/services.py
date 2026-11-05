import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from booking.models import Booking, booking_display_name
from notifications.events import notify_payment_failed, notify_payment_held

from .calculations import calculate_booking_money
from django.conf import settings

from .flutterwave import PAYMENT_OPTIONS, FlutterwaveClient, FlutterwaveUnknownError
from .models import EscrowPayment, EscrowRelease
from .ledger import record_financial_event

logger = logging.getLogger(__name__)


def record_payment_ledger(*, payment, event_type, event_key, status_before="", status_after="", actor=None, amount_kobo=None, metadata=None):
    return record_financial_event(payment=payment, event_type=event_type, event_key=event_key, status_before=status_before, status_after=status_after, actor=actor, amount_kobo=amount_kobo, metadata=metadata)


# Flutterwave's hosted page carries its own countdown (15 minutes), and the
# methods offered by a link are fixed when that link is minted. Re-using the
# stored link forever therefore does two bad things: it hands the customer a
# dead page later, and it keeps showing whatever PayPal-or-card mix existed the
# first time. Anything older than the countdown, or minted for a different
# method set, is retired and minted again.
DEFAULT_CHECKOUT_LINK_TTL_SECONDS = 900
CHECKOUT_METHODS_MARKER = "_checkout"


def checkout_link_ttl_seconds():
    return int(getattr(settings, "FLW_CHECKOUT_LINK_TTL_SECONDS", DEFAULT_CHECKOUT_LINK_TTL_SECONDS))


def checkout_link_state(payment):
    """What the provider gave us, and whether it still reflects this build."""
    payload = payment.provider_payload if isinstance(payment.provider_payload, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    record = payload.get(CHECKOUT_METHODS_MARKER) if isinstance(payload.get(CHECKOUT_METHODS_MARKER), dict) else {}
    minted_at = None
    raw_minted = str(record.get("minted_at") or "").strip().replace("Z", "+00:00")
    if raw_minted:
        try:
            minted_at = datetime.fromisoformat(raw_minted)
        except ValueError:
            minted_at = None
    if minted_at is not None and minted_at.tzinfo is None:
        minted_at = minted_at.replace(tzinfo=timezone.utc)
    return {
        "link": str(data.get("link") or data.get("checkout_url") or "").strip(),
        "minted_at": minted_at,
        "methods": str(record.get("methods") or "").strip(),
    }


def checkout_link_is_current(payment, *, now=None):
    """A link is reusable only while it is live and was minted for today's methods."""
    if not payment.provider_transaction_id:
        return False
    state = checkout_link_state(payment)
    if not state["link"] or state["minted_at"] is None:
        return False
    if state["methods"] != PAYMENT_OPTIONS:
        return False
    now = now or timezone.now()
    return (now - state["minted_at"]).total_seconds() < checkout_link_ttl_seconds()


def rotate_checkout_reference(payment, *, reason):
    """Retire the provider reference so the next attempt mints a fresh link.

    The old reference is dropped deliberately, exactly like the FAILED reset: a
    callback for the retired attempt must never attach itself to the new one.
    """
    old_status = payment.status
    prefix = "PAY-ADDON" if payment.addon_order_id else "PAY"
    owner = payment.addon_order_id or payment.booking_id
    payment.reference = f"{prefix}-{owner}-{uuid.uuid4().hex[:16]}"
    payment.provider_transaction_id = None
    payment.provider_reference = ""
    payment.provider_status = ""
    payment.provider_payload = {}
    payment.failure_reason = ""
    payment.save(
        update_fields=[
            "reference",
            "provider_transaction_id",
            "provider_reference",
            "provider_status",
            "provider_payload",
            "failure_reason",
            "updated_at",
        ]
    )
    record_payment_ledger(
        payment=payment,
        event_type="CHECKOUT_LINK_ROTATED",
        event_key=f"payment:{payment.id}:rotate:{payment.updated_at.timestamp()}",
        status_before=old_status,
        status_after=payment.status,
        metadata={"reason": str(reason)[:120]},
    )
    return payment


class PaymentVerificationPending(Exception):
    """Raised when Flutterwave has not reached a terminal payment state."""


def prepare_booking_payment(*, booking):
    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).select_related("customer__user", "artisan").get(pk=booking.pk)
        if booking.status == Booking.Status.CANCELLED:
            raise ValueError(
                "This booking was cancelled. Its payment link is no longer valid and no payment is due."
            )
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
            if payment.provider_transaction_id and not checkout_link_is_current(payment):
                # A dead link, or one minted before the checkout was restricted to
                # card and bank transfer: retire it so this click mints a fresh one.
                rotate_checkout_reference(payment, reason="stale_checkout_link")
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
            if payment.provider_transaction_id and not checkout_link_is_current(payment):
                rotate_checkout_reference(payment, reason="stale_checkout_link")
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
    if payment.link_voided_at:
        raise ValueError(
            "This payment link was voided because the booking was cancelled."
        )
    if payment.booking_id and payment.booking.status != (
        Booking.Status.IN_PROGRESS if payment.addon_order_id else Booking.Status.ACCEPTED
    ):
        raise ValueError(
            "This booking is no longer payable, so no payment link was created."
        )
    customer = payment.customer
    result = FlutterwaveClient().initialize_payment(
        reference=payment.reference,
        amount_ngn=payment.total_amount_kobo // 100,
        customer=customer,
        booking_id=payment.booking_id,
        addon_order_id=payment.addon_order_id,
        booking_label=booking_display_name(payment.booking),
    )
    provider_data = result.get("data") or {}
    with transaction.atomic():
        locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
        locked.provider_transaction_id = str(provider_data.get("id")) if provider_data.get("id") else locked.provider_transaction_id
        locked.provider_reference = locked.reference
        locked.provider_payload = {
            **(result if isinstance(result, dict) else {"response": result}),
            CHECKOUT_METHODS_MARKER: {
                "minted_at": timezone.now().isoformat(),
                "methods": PAYMENT_OPTIONS,
            },
        }
        locked.provider_status = str(result.get("status") or "initialized")[:50]
        locked.save(update_fields=["provider_transaction_id", "provider_reference", "provider_payload", "provider_status", "updated_at"])
        return locked, result

def void_booking_payment_link(*, payment, reason):
    """Neuter an unpaid payment link and mark the payment CANCELLED.

    The provider reference is kept so a late callback is still matched: if money
    really moved, it is recorded HELD with a refund-review note instead of being
    dropped. No new checkout can be minted for it, and it can never reactivate
    the cancelled booking.
    """
    if payment is None:
        return None
    if payment.status in {
        EscrowPayment.Status.HELD,
        EscrowPayment.Status.DISPUTED,
        EscrowPayment.Status.RELEASE_PENDING,
        EscrowPayment.Status.RELEASED,
    }:
        # Money is already in escrow; the refund/dispute process owns it.
        return payment
    if payment.link_voided_at and payment.status == EscrowPayment.Status.CANCELLED:
        return payment
    old_status = payment.status
    payment.link_voided_at = payment.link_voided_at or timezone.now()
    payment.link_void_reason = str(reason or "")[:200]
    if payment.status != EscrowPayment.Status.CANCELLED:
        payment.status = EscrowPayment.Status.CANCELLED
    payment.save(
        update_fields=["link_voided_at", "link_void_reason", "status", "updated_at"]
    )
    record_payment_ledger(
        payment=payment,
        event_type="PAYMENT_LINK_VOIDED",
        event_key=f"payment:{payment.id}:voided",
        status_before=old_status,
        status_after=payment.status,
    )
    return payment


# Payment states in which money is (or may already be) committed to this booking.
COMMITTED_PAYMENT_STATUSES = frozenset(
    {
        EscrowPayment.Status.HELD,
        EscrowPayment.Status.DISPUTED,
        EscrowPayment.Status.RELEASE_PENDING,
        EscrowPayment.Status.RELEASED,
    }
)


def base_payment_for(booking):
    return (
        EscrowPayment.objects.filter(booking=booking, addon_order__isnull=True)
        .order_by("created_at")
        .first()
    )


BANK_TRANSFER_MARKER = "bank_transfer"
# The provider mints a fresh virtual account per charge and keeps it open for
# roughly an hour. We hand the payer a shorter, honest window -- the same
# checkout TTL the rest of the rail uses -- so a transfer is never started on an
# account that is about to close under them. One window, one countdown: the card
# the payer reads and the rule the API enforces are the same number.
BANK_TRANSFER_WINDOW_EXTRA_SECONDS = 0


def bank_transfer_details(payment):
    """The account the payer transfers into, as last minted, or empty fields."""
    payload = payment.provider_payload if isinstance(payment.provider_payload, dict) else {}
    record = payload.get(BANK_TRANSFER_MARKER)
    record = record if isinstance(record, dict) else {}
    minted_at = None
    raw = str(record.get("minted_at") or "").strip().replace("Z", "+00:00")
    if raw:
        try:
            minted_at = datetime.fromisoformat(raw)
        except ValueError:
            minted_at = None
    if minted_at is not None and minted_at.tzinfo is None:
        minted_at = minted_at.replace(tzinfo=timezone.utc)
    return {
        "account_number": str(record.get("account_number") or "").strip(),
        "bank": str(record.get("bank") or "").strip(),
        "account_name": str(record.get("account_name") or "").strip(),
        "amount": str(record.get("amount") or "").strip(),
        "note": str(record.get("note") or "").strip(),
        "expires_at": str(record.get("expires_at") or "").strip(),
        "minted_at": minted_at,
    }


def bank_transfer_deadline(payment):
    """When the account we gave the payer stops being usable, or None."""
    details = bank_transfer_details(payment)
    if not details["account_number"] or details["minted_at"] is None:
        return None
    seconds = checkout_link_ttl_seconds() + BANK_TRANSFER_WINDOW_EXTRA_SECONDS
    return details["minted_at"] + timedelta(seconds=seconds)


def bank_transfer_is_current(payment, *, now=None):
    """An account is usable inside its window and only while the money is owed."""
    if getattr(payment, "status", None) not in (None, EscrowPayment.Status.PAYMENT_PENDING):
        return False
    deadline = bank_transfer_deadline(payment)
    if deadline is None:
        return False
    return (now or timezone.now()) < deadline


def bank_transfer_window(payment, *, now=None):
    """The live bank-transfer window, as the card, the button and the API see it.

    ``live`` answers one question -- may the payer still send money to the
    account on screen -- and it is the single answer used by the serializer, the
    chat template and the renewal guard, so the countdown, the hidden button and
    the refusal can never disagree.
    """
    now = now or timezone.now()
    deadline = bank_transfer_deadline(payment)
    if deadline is None:
        return {"live": False, "until": "", "seconds_left": 0}
    left = int((deadline - now).total_seconds())
    live = left > 0 and bank_transfer_is_current(payment, now=now)
    return {
        "live": live,
        "until": deadline.isoformat() if live else "",
        "seconds_left": max(0, left),
    }


def ensure_bank_transfer_charge(payment):
    """Return a live virtual account for this payment, minting one only if needed.

    One account per payment reference: minting again would give the payer a
    second account number for the same money, and the provider matches the
    transfer to the reference anyway.
    """
    if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
        raise ValueError("This payment is not waiting for money.")
    if payment.link_voided_at:
        raise ValueError("This payment link was voided with the booking.")
    if bank_transfer_is_current(payment):
        return bank_transfer_details(payment)
    if bank_transfer_details(payment)["account_number"]:
        # The account on screen has closed. The provider will not charge the
        # same reference twice, so the closed attempt retires with its own
        # reference and the new account is minted against a fresh one -- the
        # payer is never given a second number for the same reference.
        payment = rotate_checkout_reference(
            payment, reason="bank_transfer_expired"
        )

    customer = payment.customer
    result = FlutterwaveClient().create_bank_transfer_charge(
        reference=payment.reference,
        amount_ngn=payment.total_amount_kobo // 100,
        customer=customer,
        booking_id=payment.booking_id,
        booking_label=booking_display_name(payment.booking),
    )
    authorization = (result.get("meta") or {}).get("authorization") or {}
    account_number = str(authorization.get("transfer_account") or "").strip()
    if not account_number:
        raise ValueError("The provider did not return an account to transfer into.")

    record = {
        "account_number": account_number,
        "bank": str(authorization.get("transfer_bank") or "").strip(),
        "account_name": str(payment.customer.get_full_name() or "").strip(),
        "amount": str(authorization.get("transfer_amount") or "").strip(),
        "note": str(authorization.get("transfer_note") or "").strip(),
        "expires_at": str(authorization.get("account_expiration") or "").strip(),
        "transfer_reference": str(authorization.get("transfer_reference") or "").strip(),
        "minted_at": timezone.now().isoformat(),
    }
    with transaction.atomic():
        locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
        payload = locked.provider_payload if isinstance(locked.provider_payload, dict) else {}
        locked.provider_payload = {**payload, BANK_TRANSFER_MARKER: record}
        locked.provider_reference = locked.reference
        locked.save(update_fields=["provider_payload", "provider_reference", "updated_at"])
    return bank_transfer_details(locked)


def renew_bank_transfer_charge(payment):
    """A fresh account for a payer whose last one closed before they paid.

    The provider will not mint a second account against a reference it has
    already charged, so the closed attempt is retired with its reference -- the
    exact same rule as a stale checkout link -- and a new charge is minted. The
    old account has expired, so nothing can land on it any more.
    """
    if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
        raise ValueError("This payment is not waiting for money.")
    if payment.link_voided_at:
        raise ValueError("This payment link was voided with the booking.")
    window = bank_transfer_window(payment)
    if window["live"]:
        # Frontend and backend say the same thing: one live account at a time.
        # Minting a second one now would leave the payer with two numbers and
        # two chances to send the same money twice.
        raise ValueError(
            "The transfer account on screen is still open. Use it, or wait for "
            "the timer to finish before asking for a new one."
        )
    return ensure_bank_transfer_charge(payment)


# A bank transfer completes silently: the money lands in the provider's account
# and, in production, a webhook tells us. A local or staging install has no
# public callback and no worker, so the payer's own poll asks the provider --
# otherwise someone who really paid keeps reading "payment is the next step".
AUTO_VERIFY_INTERVAL_SECONDS = 20


def auto_verify_pending_transfer(payment):
    """Settle a bank transfer as soon as the provider can see the money.

    Called from the payer's own payment poll while the transfer card is on
    screen. One provider call per payment per 20 seconds at most; everything
    else (no minted account, already settled, provider still waiting) returns
    the untouched payment so the page simply keeps polling.
    """
    if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
        return payment
    if not bank_transfer_details(payment)["account_number"]:
        return payment
    from django.core.cache import cache

    if not cache.add(
        f"payment:auto-verify:{payment.id}",
        1,
        timeout=AUTO_VERIFY_INTERVAL_SECONDS,
    ):
        return payment
    try:
        return verify_and_hold_payment(payment.id)
    except PaymentVerificationPending:
        return payment
    except Exception:  # provider down, unknown outcome: never break the poll
        logger.warning(
            "bank_transfer_auto_verify_failed payment=%s", payment.id, exc_info=True
        )
        return payment


def ensure_checkout_link(payment):
    """Ask the provider for a checkout when a pending payment has no usable one.

    The payer's page polls the payment record every few seconds, so this is the
    one place that can heal a payment whose first initialization failed, whose
    link expired, or whose link was minted before checkout was restricted. A
    short cache lock keeps the poll from stampeding the provider.
    """
    if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
        return False
    if payment.link_voided_at:
        return False
    if checkout_link_is_current(payment):
        return False
    from django.core.cache import cache

    lock_key = f"payment:renew-requested:{payment.id}"
    if not cache.add(lock_key, 1, timeout=45):
        return False
    try:
        from .tasks import initialize_prepared_payment

        initialize_prepared_payment.delay(str(payment.id))
    except Exception:  # broker down: the next poll tries again
        cache.delete(lock_key)
        return False
    return True


def initialize_booking_payment(*, booking):
    payment = prepare_booking_payment(booking=booking)
    if payment.status == EscrowPayment.Status.HELD:
        return payment, None
    if payment.provider_transaction_id:
        if checkout_link_is_current(payment):
            return payment, payment.provider_payload
        with transaction.atomic():
            payment = rotate_checkout_reference(payment, reason="stale_checkout_link")
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
        # A cancelled link is still verifiable: if the provider confirms money, it
        # must be recorded (HELD + refund review) rather than silently dropped.
        if payment.status not in {
            EscrowPayment.Status.PAYMENT_PENDING,
            EscrowPayment.Status.CANCELLED,
        }:
            raise ValueError("Payment is not awaiting verification.")
        # A bank-transfer charge has no transaction id until the money lands, so
        # the reference is enough: the provider resolves it for us below.
        provider_transaction_id = (
            payment.provider_transaction_id
            or (str(provider_transaction_id) if provider_transaction_id else "")
        )
        expected_reference = payment.reference
        expected_amount = payment.total_amount_kobo // 100

    if provider_transaction_id:
        result = FlutterwaveClient().verify_transaction(provider_transaction_id)
    else:
        result = FlutterwaveClient().verify_transaction_by_reference(expected_reference)
    data = result.get("data") or {}
    provider_status = str(data.get("status", "")).lower()
    provider_reference = str(data.get("tx_ref", "")).strip()
    provider_currency = str(data.get("currency", "")).upper()
    provider_amount = Decimal(str(data.get("amount", "0")))
    provider_id = str(data.get("id", ""))
    if not provider_transaction_id and provider_id:
        provider_transaction_id = provider_id

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
            if payment.status not in {EscrowPayment.Status.PAYMENT_PENDING, EscrowPayment.Status.CANCELLED}:
                return payment
            if payment.status == EscrowPayment.Status.CANCELLED:
                # Nothing moved; the cancellation stands.
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
        if payment.status not in {EscrowPayment.Status.PAYMENT_PENDING, EscrowPayment.Status.CANCELLED}:
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
        payment.provider_transaction_id = payment.provider_transaction_id or provider_id
        payment.save(update_fields=["status", "provider_status", "verification_payload", "verified_at", "held_at", "failure_reason", "provider_transaction_id", "updated_at"])
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
