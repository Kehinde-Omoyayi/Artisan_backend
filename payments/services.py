import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from booking.models import Booking, booking_display_name
from notifications.events import notify_payment_failed, notify_payment_held

from .calculations import calculate_booking_money
from django.conf import settings

from .flutterwave import PAYMENT_OPTIONS, FlutterwaveClient, FlutterwaveError, FlutterwaveUnknownError
from .models import EscrowPayment, EscrowRelease, PaymentBankTransfer
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
PROVIDER_AMOUNT_REFUSED = "provider_amount_mismatch"


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
        minted_at = minted_at.replace(tzinfo=dt_timezone.utc)
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
# A creation attempt is reserved in the database *before* the provider is
# called, and the reservation is released (or marked OUTCOME_UNKNOWN) when the
# call returns. Anything older than this is presumed dead: the worker crashed
# mid-flight and the truth has to come from the provider, never from a guess.
CREATING_RESERVATION_TIMEOUT_SECONDS = 120


class BankTransferOutcomeUnknown(Exception):
    """The provider did not say whether the account was created.

    The attempt stays on the original reference and is reconciled later; the
    payer is never handed a second account because one request timed out.
    """


def _parse_provider_datetime(raw):
    """Normalise a provider timestamp to an aware UTC datetime.

    Flutterwave documents ``account_expiration`` as the closing time of the
    virtual account. Its payloads carry values like ``2026-06-10 19:00:16``
    with no offset; those are UTC. An explicit offset, when present, is honoured
    and converted instead of discarded.
    """
    text = str(raw or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    value = None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                value = datetime.strptime(text, pattern)
                break
            except ValueError:
                value = None
    if value is None:
        return None
    if timezone.is_naive(value):
        # No offset in the payload: the provider's clock is UTC.
        return timezone.make_aware(value, dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def _naira_to_kobo(raw):
    try:
        amount = Decimal(str(raw).replace(",", "").strip())
    except Exception:
        return None
    return int((amount * 100).quantize(Decimal("1")))


def _kobo_to_naira_str(kobo):
    amount = Decimal(int(kobo)) / Decimal(100)
    return f"{amount:.2f}"


def _transfer_row(payment):
    """The state machine row for this payment, or None.

    Read fresh each time: the row is the financial truth and can be changed by
    another worker at any moment. Callers that already hold the row under a
    lock use it directly instead of calling this.
    """
    if payment is None or not payment.pk:
        return None
    return PaymentBankTransfer.objects.filter(payment_id=payment.pk).first()


def _transfer_details_from_row(row):
    if row is None:
        return {
            "account_number": "",
            "bank": "",
            "account_name": "",
            "beneficiary_name": "",
            "payer_name": "",
            "narration": "",
            "amount": "",
            "amount_kobo": None,
            "currency": "NGN",
            "reference": "",
            "transfer_reference": "",
            "expires_at": "",
            "minted_at": None,
            "mode": "",
            "note": "",
            "status": PaymentBankTransfer.Status.NOT_CREATED,
        }
    amount = ""
    if row.transfer_amount_kobo is not None:
        amount = _kobo_to_naira_str(row.transfer_amount_kobo)
    return {
        "account_number": row.transfer_account or "",
        "bank": row.transfer_bank or "",
        # Rows stored before the fallback chain existed carry an empty
        # beneficiary even though the provider told the payer the name; derive
        # it from the stored provider instruction instead of showing "—".
        "account_name": row.beneficiary_name or _account_name_from_authorization(
            {"transfer_note": row.transfer_note, "narration": row.narration}
        ),
        "beneficiary_name": row.beneficiary_name or _account_name_from_authorization(
            {"transfer_note": row.transfer_note, "narration": row.narration}
        ),
        "payer_name": row.payer_name or "",
        "narration": row.narration or "",
        "amount": amount,
        "amount_kobo": row.transfer_amount_kobo,
        "currency": "NGN",
        "reference": row.reference or "",
        "transfer_reference": row.transfer_reference or "",
        "expires_at": timezone.localtime(row.account_expires_at).isoformat() if row.account_expires_at else "",
        "minted_at": row.minted_at,
        "mode": row.mode or "",
        "note": row.transfer_note or "",
        "status": row.status,
    }


def bank_transfer_details(payment):
    """The account the payer transfers into, as the state machine knows it."""
    return _transfer_details_from_row(_transfer_row(payment))


def bank_transfer_deadline(payment):
    """When the provider's account stops being usable, or None."""
    row = _transfer_row(payment)
    if row is None:
        return None
    if not row.transfer_account and row.status in {
        PaymentBankTransfer.Status.NOT_CREATED,
    }:
        return None
    return row.account_expires_at


def bank_transfer_is_current(payment, *, now=None):
    """An account is usable only while the provider says it is open."""
    if getattr(payment, "status", None) not in (None, EscrowPayment.Status.PAYMENT_PENDING):
        return False
    row = _transfer_row(payment)
    if row is None or row.status != PaymentBankTransfer.Status.READY:
        return False
    if not row.transfer_account or row.account_expires_at is None:
        return False
    return (now or timezone.now()) < row.account_expires_at


def bank_transfer_window(payment, *, now=None):
    """The live bank-transfer window, as the card, the button and the API see it."""
    now = now or timezone.now()
    row = _transfer_row(payment)
    deadline = row.account_expires_at if row else None
    if row is None or not row.transfer_account or deadline is None:
        return {"live": False, "until": "", "seconds_left": 0}
    left = int((deadline - now).total_seconds())
    live = left > 0 and bank_transfer_is_current(payment, now=now)
    return {
        "live": live,
        "until": deadline.isoformat() if live else "",
        "seconds_left": max(0, left),
    }


def _bt_response(row, *, extra=None):
    details = _transfer_details_from_row(row)
    response = {
        "account_number": details["account_number"],
        "bank": details["bank"],
        "account_name": details["beneficiary_name"],
        "beneficiary_name": details["beneficiary_name"],
        "payer_name": details["payer_name"],
        "narration": details["narration"],
        "amount": details["amount"],
        "amount_kobo": details["amount_kobo"],
        "currency": details["currency"],
        "reference": details["reference"],
        "transfer_reference": details["transfer_reference"],
        "expires_at": details["expires_at"],
        "minted_at": details["minted_at"].isoformat() if details["minted_at"] else "",
        "mode": details["mode"],
        "note": details["note"],
        "status": details["status"],
    }
    if extra:
        response.update(extra)
    return response


def _lock_payment_and_row(payment_pk, row_pk=None):
    locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment_pk)
    if row_pk is not None:
        row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row_pk)
    else:
        row, _ = PaymentBankTransfer.objects.select_for_update(of=("self",)).get_or_create(
            payment=locked,
            defaults={
                "reference": locked.reference,
                "status": PaymentBankTransfer.Status.NOT_CREATED,
            },
        )
    return locked, row


def _reserve_for_mint_or_reuse(payment):
    """Decide, under both row locks, what this call may do.

    Returns a decision string plus the locked rows. The provider is never
    called while this transaction (or the locks) are held.
    """
    now = timezone.now()
    with transaction.atomic():
        locked, row = _lock_payment_and_row(payment.pk)
        if locked.status != EscrowPayment.Status.PAYMENT_PENDING:
            raise ValueError("This payment is not waiting for money.")
        if locked.link_voided_at:
            raise ValueError("This payment link was voided with the booking.")
        # Keep the attempt reference in step with a payment whose reference was
        # rotated after a failed/expired attempt.
        if row.status in {PaymentBankTransfer.Status.NOT_CREATED, PaymentBankTransfer.Status.FAILED} and row.reference != locked.reference and not row.transfer_account:
            row.reference = locked.reference
            row.save(update_fields=["reference", "updated_at"])
        if row.status == PaymentBankTransfer.Status.READY:
            if row.account_expires_at and row.account_expires_at > now:
                return "ready", locked, row
            return "expired", locked, row
        if row.status == PaymentBankTransfer.Status.CREATING:
            started = row.creation_started_at
            if started and (now - started).total_seconds() < CREATING_RESERVATION_TIMEOUT_SECONDS:
                return "processing", locked, row
            # The reservation outlived any honest attempt: the outcome is not
            # ours to guess. Reconcile before anything else happens.
            row.status = PaymentBankTransfer.Status.OUTCOME_UNKNOWN
            row.last_error = "creation_reservation_timed_out"
            row.save(update_fields=["status", "last_error", "updated_at"])
            return "reconcile", locked, row
        if row.status == PaymentBankTransfer.Status.OUTCOME_UNKNOWN:
            return "reconcile", locked, row
        if row.status == PaymentBankTransfer.Status.EXPIRED:
            return "mint", locked, row
        if (
            row.status == PaymentBankTransfer.Status.FAILED
            and (row.last_error or "") == PROVIDER_AMOUNT_REFUSED
        ):
            # A refused attempt may have created an account the payer never
            # saw. Retired references must never be reused for the next
            # charge, so the next attempt mints against a fresh one.
            locked = rotate_checkout_reference(locked, reason="provider_amount_refused")
            row.reference = locked.reference
            row.last_error = ""
            row.save(update_fields=["reference", "last_error", "updated_at"])
        # NOT_CREATED or FAILED (provider refused; nothing exists to recover)
        return "mint", locked, row


def _mark_outcome_unknown(row_pk, reason):
    with transaction.atomic():
        row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row_pk)
        row.status = PaymentBankTransfer.Status.OUTCOME_UNKNOWN
        row.last_error = str(reason or "unknown")[:200]
        row.save(update_fields=["status", "last_error", "updated_at"])


def _persist_ready(*, payment_pk, row_pk, result):
    """Persist a provider answer as the one true attempt for this payment."""
    # The create call answers with a top-level ``meta.authorization``; a
    # verify-by-reference answer nests the same block under ``data.meta``.
    # Accept both -- the heal path hands us a verify-shaped result.
    authorization = (result.get("meta") or {}).get("authorization") or (
        ((result.get("data") or {}).get("meta") or {}).get("authorization")
    ) or {}
    account_number = str(authorization.get("transfer_account") or "").strip()
    if not account_number:
        raise _MissingAccountDetails("The provider did not return an account to transfer into.")
    expires_at = _parse_provider_datetime(authorization.get("account_expiration"))
    now = timezone.now()
    provider_kobo = _naira_to_kobo(authorization.get("transfer_amount"))
    # Flutterwave adds its own charges to the amount the payer must send: the
    # authorization block carries amount + fee (₦155.04 requested for a ₦152.00
    # payment -- exactly 2%). That figure is the one the payer is told to pay,
    # so an answer that covers the escrow within a bounded fee is accepted, and
    # the escrow check in verify_and_hold_payment keys off the charge amount
    # (verification payload ``amount``), not this figure.
    #
    # Anything outside the band -- less than the escrow, or implausibly more
    # than the escrow -- is a provider bug or a hostile response. It is a
    # definitive refusal, and the reservation must be retired durably before
    # the error leaves this function: a refusal rolled back with the same
    # transaction that recorded it would leave the payment stuck in CREATING.
    if provider_kobo is not None:
        refused = False
        with transaction.atomic():
            probe_locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment_pk)
            probe_expected = (probe_locked.total_amount_kobo // 100) * 100
            excess_cap_kobo = max(10_000, (probe_expected * 2) // 100)  # 10,000 kobo = ₦100 flat, or 2% of the payment, whichever is larger
            excess = provider_kobo - probe_expected
            if excess < 0 or excess > excess_cap_kobo:
                probe_row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row_pk)
                probe_row.status = PaymentBankTransfer.Status.FAILED
                probe_row.last_error = PROVIDER_AMOUNT_REFUSED
                probe_row.save(update_fields=["status", "last_error", "updated_at"])
                record_payment_ledger(
                    payment=probe_locked,
                    event_type="TRANSFER_ACCOUNT_AMOUNT_MISMATCH",
                    event_key=f"payment:{probe_locked.id}:transfer-amount-mismatch:{probe_row.attempts}",
                    metadata={
                        "provider_kobo": provider_kobo,
                        "expected_kobo": probe_expected,
                        "excess_kobo": excess,
                        "excess_cap_kobo": excess_cap_kobo,
                    },
                )
                refused = True
        if refused:
            # The provider may have created this account already, so the
            # reference that minted it is retired: the next attempt runs
            # against a fresh reference instead of colliding with a live,
            # unseen charge at the provider. The old account expires alone
            # (never displayed, never payable).
            rotate_checkout_reference(
                EscrowPayment.objects.get(pk=payment_pk),
                reason="provider_amount_refused",
            )
            raise ValueError(
                "The provider returned an amount that does not match this payment. "
                "Nothing has been charged against a different figure."
            )
    with transaction.atomic():
        locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment_pk)
        row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row_pk)
        # The payment may have moved on while the provider was answering. The
        # account still existed when the provider minted it, so the details are
        # kept for a late transfer -- but the attempt is no longer offered.
        payable = (
            locked.status == EscrowPayment.Status.PAYMENT_PENDING
            and not locked.link_voided_at
        )
        expected_kobo = (locked.total_amount_kobo // 100) * 100
        row.transfer_account = account_number[:34]
        row.transfer_bank = str(authorization.get("transfer_bank") or "").strip()[:150]
        row.transfer_reference = str(authorization.get("transfer_reference") or "").strip()[:150]
        # The provider's own closing time is the only clock that means anything.
        # If it is missing we fall back to one hour (their documented maximum),
        # never to a locally invented 15-minute countdown.
        row.account_expires_at = expires_at or (now + timedelta(hours=1))
        row.minted_at = now
        row.transfer_amount_kobo = expected_kobo if provider_kobo is None else provider_kobo
        row.mode = str(authorization.get("mode") or "").strip()[:40]
        row.transfer_note = str(authorization.get("transfer_note") or "").strip()[:200]
        row.narration = str(authorization.get("narration") or "").strip()[:200]
        row.payer_name = str(getattr(locked.customer, "get_full_name", lambda: "")() or "").strip()[:150]
        row.beneficiary_name = _account_name_from_authorization(authorization)
        row.last_error = "" if expires_at else "provider_expiry_missing_assumed_one_hour"
        row.status = (
            PaymentBankTransfer.Status.READY
            if payable
            else PaymentBankTransfer.Status.EXPIRED
        )
        row.save()
        locked.provider_reference = locked.reference
        locked.provider_payload = {
            **(locked.provider_payload if isinstance(locked.provider_payload, dict) else {}),
            BANK_TRANSFER_MARKER: {
                "account_number": row.transfer_account,
                "bank": row.transfer_bank,
                "beneficiary_name": row.beneficiary_name,
                "payer_name": row.payer_name,
                "narration": row.narration,
                "amount": str(authorization.get("transfer_amount") or ""),
                "note": row.transfer_note,
                "expires_at": row.account_expires_at.isoformat() if row.account_expires_at else "",
                "transfer_reference": row.transfer_reference,
                "mode": row.mode,
                "minted_at": row.minted_at.isoformat() if row.minted_at else "",
            },
        }
        locked.save(update_fields=["provider_payload", "provider_reference", "updated_at"])
        if not payable:
            record_payment_ledger(
                payment=locked,
                event_type="TRANSFER_ACCOUNT_AFTER_CHANGE",
                event_key=f"payment:{locked.id}:transfer-late-create:{row.attempts}",
                metadata={"attempts": row.attempts},
            )
        else:
            record_payment_ledger(
                payment=locked,
                event_type="TRANSFER_ACCOUNT_MINTED",
                event_key=f"payment:{locked.id}:transfer-minted:{row.attempts}",
                amount_kobo=locked.total_amount_kobo,
                metadata={
                    "reference": row.reference,
                    "attempt": row.attempts,
                    "fee_kobo": max(0, provider_kobo - expected_kobo) if provider_kobo is not None else 0,
                },
            )
    row.refresh_from_db()
    return row


_NOTE_PREFIXES = ("please make a bank transfer to ",)
_NOTE_SUFFIXES = (".", " ")

def _account_name_from_authorization(authorization):
    """The name the payer will see at their bank for this account.

    Flutterwave's authorization block usually carries no beneficiary field at
    all, so the name the payer's bank shows for the virtual account is taken
    from the instruction the provider itself sends: the transfer note ("Please
    make a bank transfer to <name>"), falling back to the narration we send,
    which the provider builds that note from. The payer's own name is never
    used as the account name. Returns "" when nothing provider-authored is
    available -- the UI then shows "—".
    """
    named = (
        authorization.get("account_name")
        or authorization.get("beneficiary_name")
        or authorization.get("account_beneficiary")
    )
    if named:
        return str(named).strip()[:150]
    note = str(authorization.get("transfer_note") or "").strip()
    lowered = note.lower()
    for prefix in _NOTE_PREFIXES:
        if lowered.startswith(prefix):
            candidate = note[len(prefix):].strip().rstrip(". ")
            if candidate:
                return candidate[:150]
    narration = str(authorization.get("narration") or "").strip()
    if narration:
        return narration[:150]
    return ""


class _MissingAccountDetails(Exception):
    pass


def heal_unknown_attempt(locked, row):
    """Ask the provider what happened to an attempt whose answer was lost.

    Read-only questions, on the *original* reference. A fresh charge is never
    created while the outcome is unknown.
    """
    try:
        result = FlutterwaveClient().verify_transaction_by_reference(row.reference)
    except FlutterwaveUnknownError:
        return _bt_response(row, extra={"live": False, "state": "OUTCOME_UNKNOWN"})
    except FlutterwaveError as exc:
        if _is_provider_not_found(exc):
            # The provider has never heard of this reference: the charge did
            # not happen and the reservation can be retired safely.
            with transaction.atomic():
                locked, row = _lock_payment_and_row(locked.pk, row.pk)
                if row.status == PaymentBankTransfer.Status.OUTCOME_UNKNOWN:
                    row.status = PaymentBankTransfer.Status.FAILED
                    row.last_error = "provider_confirmed_missing"
                    row.save(update_fields=["status", "last_error", "updated_at"])
                    record_payment_ledger(
                        payment=locked,
                        event_type="TRANSFER_ATTEMPT_ABANDONED",
                        event_key=f"payment:{locked.id}:transfer-abandoned:{row.attempts}",
                        metadata={"reference": row.reference},
                    )
            return None
        return _bt_response(row, extra={"live": False, "state": "OUTCOME_UNKNOWN"})
    data = result.get("data") or {}
    authorization = (data.get("meta") or {}).get("authorization") or {}
    if str(authorization.get("transfer_account") or "").strip():
        healed_row = _persist_ready(payment_pk=locked.pk, row_pk=row.pk, result=result)
        return _bt_response(healed_row, extra={"state": healed_row.status})
    provider_status = str(data.get("status") or "").lower()
    if provider_status == "successful":
        # Money moved: hand over to the normal verification path, which locks
        # booking and payment and records escrow.
        try:
            verify_and_hold_payment(str(locked.pk))
        except Exception:
            logger.exception("bank_transfer_late_heal_verify_failed payment=%s", locked.pk)
        with transaction.atomic():
            row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row.pk)
            row.status = PaymentBankTransfer.Status.PAID
            row.save(update_fields=["status", "updated_at"])
        return _bt_response(row, extra={"state": "PAID"})
    # The transaction exists but carries no account (or an unpaid one): the
    # account we were shown is not recoverable from this answer. Keep the
    # attempt unknown rather than guess or mint a competing one.
    logger.warning(
        "bank_transfer_outcome_unrecoverable payment=%s reference=%s provider_status=%s",
        locked.pk,
        row.reference,
        provider_status or "unknown",
    )
    return _bt_response(row, extra={"live": False, "state": "OUTCOME_UNKNOWN"})


def _prepare_fresh_mint(payment):
    """Expire/reconcile the old attempt, then allow minting a new one."""
    now = timezone.now()
    with transaction.atomic():
        locked, row = _lock_payment_and_row(payment.pk)
        expired = row.account_expires_at and row.account_expires_at <= now
        if row.status == PaymentBankTransfer.Status.EXPIRED or (
            row.status == PaymentBankTransfer.Status.READY and expired
        ):
            row.status = PaymentBankTransfer.Status.EXPIRED
            row.save(update_fields=["status", "updated_at"])
    # Reconcile the final state of the closed account before opening a new
    # one: if the provider saw money, this payment is HELD and must not be
    # reset; if it is gone or still pending, the account can never receive
    # again (its provider expiry has passed) and minting is safe.
    try:
        result = FlutterwaveClient().verify_transaction_by_reference(row.reference)
        provider_status = str((result.get("data") or {}).get("status") or "").lower()
    except FlutterwaveUnknownError:
        # We cannot say what happened. Do not rotate the reference, do not
        # mint: mark the attempt unknown and let the reconciler finish.
        _mark_outcome_unknown(row.pk, "expiry_reconcile_failed")
        return None
    except FlutterwaveError as exc:
        provider_status = "missing" if _is_provider_not_found(exc) else "unknown"
    if provider_status == "successful":
        try:
            verify_and_hold_payment(str(locked.pk))
        except Exception:
            logger.exception("bank_transfer_pre_mint_verify_failed payment=%s", locked.pk)
        with transaction.atomic():
            fresh = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row.pk)
            fresh.status = PaymentBankTransfer.Status.PAID
            fresh.save(update_fields=["status", "updated_at"])
        raise ValueError("This payment has already been confirmed by the provider.")
    # No money: rotate the payment reference so the closed attempt retires
    # with its own reference and the new account mints against a fresh one.
    rotated = rotate_checkout_reference(locked, reason="bank_transfer_expired")
    with transaction.atomic():
        row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row.pk)
        row.status = PaymentBankTransfer.Status.EXPIRED
        row.reference = rotated.reference
        row.transfer_account = ""
        row.transfer_bank = ""
        row.transfer_reference = ""
        row.beneficiary_name = ""
        row.transfer_note = ""
        row.narration = ""
        row.transfer_amount_kobo = None
        row.mode = ""
        row.account_expires_at = None
        row.minted_at = None
        row.last_error = ""
        row.save()
    return rotated


def ensure_bank_transfer_charge(payment):
    """Return the live virtual account for this payment, minting one only if needed.

    One account per payment at a time. The account already minted (and still
    within its provider window) is returned untouched; a request already in
    flight is reported as processing; an outcome the provider never confirmed is
    reconciled on the original reference instead of being re-minted.
    """
    decision, locked, row = _reserve_for_mint_or_reuse(payment)
    if decision == "ready":
        response = _bt_response(row)
        window = bank_transfer_window(payment)
        response.update(window)
        return response
    if decision == "processing":
        return _bt_response(
            row,
            extra={"live": False, "seconds_left": 0, "state": "CREATING"},
        )
    if decision == "reconcile":
        healed = heal_unknown_attempt(locked, row)
        if healed is not None:
            return healed
        # The provider confirmed the attempt never existed: fall through and mint.
    if decision == "expired":
        rotated = _prepare_fresh_mint(locked)
        if rotated is None:
            with transaction.atomic():
                fresh = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row.pk)
                return _bt_response(fresh, extra={"live": False, "state": "OUTCOME_UNKNOWN"})
        locked, row = _lock_payment_and_row(rotated.pk)
    # Mint, after reserving the attempt so no concurrent call repeats it.
    with transaction.atomic():
        locked = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=locked.pk)
        if locked.status != EscrowPayment.Status.PAYMENT_PENDING or locked.link_voided_at:
            raise ValueError("This payment is not waiting for money.")
        row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row.pk)
        if row.status not in {
            PaymentBankTransfer.Status.NOT_CREATED,
            PaymentBankTransfer.Status.FAILED,
            PaymentBankTransfer.Status.EXPIRED,
        }:
            # Lost the race to another worker: report the winner's state.
            return _bt_response(row, extra={"state": row.status})
        row.status = PaymentBankTransfer.Status.CREATING
        row.reference = locked.reference
        row.creation_started_at = timezone.now()
        row.attempts = (row.attempts or 0) + 1
        row.last_error = ""
        row.save(
            update_fields=[
                "status",
                "reference",
                "creation_started_at",
                "attempts",
                "last_error",
                "updated_at",
            ]
        )
    try:
        result = FlutterwaveClient().create_bank_transfer_charge(
            reference=locked.reference,
            amount_ngn=locked.total_amount_kobo // 100,
            customer=locked.customer,
            booking_id=locked.booking_id,
            booking_label=booking_display_name(locked.booking),
        )
    except FlutterwaveUnknownError:
        # The provider may well have created the account. Keep the reservation
        # honest: unknown outcome, original reference, reconcile later.
        _mark_outcome_unknown(row.pk, "provider_timeout_or_5xx")
        raise BankTransferOutcomeUnknown(
            "The provider did not confirm the transfer account. We are checking "
            "what happened before anything else is created."
        ) from None
    except FlutterwaveError as exc:
        message = str(exc).strip() or "The provider refused to mint an account."
        with transaction.atomic():
            row = PaymentBankTransfer.objects.select_for_update(of=("self",)).get(pk=row.pk)
            row.status = PaymentBankTransfer.Status.FAILED
            row.last_error = message[:200]
            row.save(update_fields=["status", "last_error", "updated_at"])
        raise ValueError(message) from None
    except Exception:
        logger.exception("bank_transfer_charge_unexpected payment=%s", locked.pk)
        _mark_outcome_unknown(row.pk, "unexpected_client_error")
        raise BankTransferOutcomeUnknown(
            "The provider did not confirm the transfer account. We are checking "
            "what happened before anything else is created."
        ) from None
    try:
        row = _persist_ready(payment_pk=locked.pk, row_pk=row.pk, result=result)
    except _MissingAccountDetails as exc:
        _mark_outcome_unknown(row.pk, "account_details_missing_from_provider")
        raise BankTransferOutcomeUnknown(str(exc)) from None
    response = _bt_response(row)
    window = bank_transfer_window(payment)
    response.update(window)
    return response


def renew_bank_transfer_charge(payment):
    """Ask for a fresh account after the previous one closed.

    The provider's own expiry is the gate: while the current account is open,
    nothing is minted. Once it has closed (and been reconciled), a new account
    is minted against a rotated reference.
    """
    if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
        raise ValueError("This payment is not waiting for money.")
    if payment.link_voided_at:
        raise ValueError("This payment link was voided with the booking.")
    if bank_transfer_is_current(payment):
        raise ValueError(
            "The transfer account on screen is still open. Use it, or wait for "
            "the timer to finish before asking for a new one."
        )
    reset_auto_verify_backoff(payment.id)
    return ensure_bank_transfer_charge(payment)


# A bank transfer completes silently: the money lands in the provider's account
# and, in production, a webhook tells us. A local or staging install has no
# public callback and no worker, so the payer's own poll asks the provider --
# otherwise someone who really paid keeps reading "payment is the next step".
#
# The poll is not free, though, and "the provider has not seen the money yet"
# is the *normal* answer for most of the window: the customer has not walked to
# their banking app yet. So the ask is spaced out, it stretches while the answer
# stays the same, and it stops altogether once the account on screen has closed
# or the provider has said "nothing here" enough times to be believed. What is
# left is a handful of provider calls per attempt instead of one every few
# seconds forever.
AUTO_VERIFY_MIN_INTERVAL_SECONDS = 20
AUTO_VERIFY_MAX_INTERVAL_SECONDS = 300
AUTO_VERIFY_GIVE_UP_AFTER = 12
# What the provider says, in its own words, when the money simply has not
# arrived. Recognised so a waiting transfer is logged as waiting, not as a
# crash: a traceback for every poll turned a normal wait into a wall of noise.
_PROVIDER_NOT_FOUND_MARKERS = (
    "no transaction was found",
    "transaction not found",
    "no transaction found",
)


def _auto_verify_keys(payment_id):
    return (
        f"payment:auto-verify:{payment_id}",
        f"payment:auto-verify-failures:{payment_id}",
    )


def _is_provider_not_found(exc):
    """True when the provider is simply saying 'nothing here yet'."""
    text = str(exc or "").lower()
    return any(marker in text for marker in _PROVIDER_NOT_FOUND_MARKERS)


def reset_auto_verify_backoff(payment_id):
    """Forget the backoff after the payer does something real.

    Asking for a fresh account, or the provider telling us money moved, means
    waiting is worth doing again from the short interval.
    """
    from django.core.cache import cache

    gate_key, failure_key = _auto_verify_keys(payment_id)
    cache.delete_many([gate_key, failure_key])


def auto_verify_pending_transfer(payment):
    """Settle a bank transfer as soon as the provider can see the money.

    Called from the payer's own payment poll while the transfer card is on
    screen. One provider call per payment per interval at most, and the interval
    stretches from 20 seconds towards 5 minutes the longer the provider keeps
    saying it has not seen anything. Once the transfer window has closed -- or
    the provider has said "nothing here" a dozen times -- the polling stops
    asking at all and the payment is left alone for the webhook or a manual
    check. The page keeps working either way; it just stops calling out.
    """
    if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
        return payment
    if not bank_transfer_details(payment)["account_number"]:
        return payment
    from django.core.cache import cache

    gate_key, failure_key = _auto_verify_keys(payment.id)
    failures = int(cache.get(failure_key) or 0)
    if failures >= AUTO_VERIFY_GIVE_UP_AFTER:
        # The money is not coming on its own. Stop calling the provider for
        # this attempt; the payer can ask again with a fresh account, which
        # resets the counter.
        return payment
    if not bank_transfer_window(payment)["live"]:
        # The account on screen has closed. Nothing can land on it now, so
        # there is nothing to wait for.
        return payment
    if not cache.add(gate_key, 1, timeout=AUTO_VERIFY_MIN_INTERVAL_SECONDS):
        return payment
    try:
        settled = verify_and_hold_payment(payment.id)
    except PaymentVerificationPending:
        return payment
    except Exception as exc:  # provider down, unknown outcome: never break the poll
        failures = int(cache.get(failure_key) or 0) + 1
        # Stretch the gap between asks: someone who has not paid yet does not
        # need the provider phoned every twenty seconds until they do.
        backoff = min(
            AUTO_VERIFY_MIN_INTERVAL_SECONDS * (2**failures),
            AUTO_VERIFY_MAX_INTERVAL_SECONDS,
        )
        cache.set(failure_key, failures, timeout=AUTO_VERIFY_MAX_INTERVAL_SECONDS * 2)
        cache.set(gate_key, 1, timeout=backoff)
        if _is_provider_not_found(exc):
            # The ordinary state while the customer walks to their banking app.
            # One quiet line, not a traceback on every poll.
            logger.info(
                "bank_transfer_awaiting_money payment=%s attempt=%s retry_in=%ss",
                payment.id,
                failures,
                backoff,
            )
        else:
            logger.warning(
                "bank_transfer_auto_verify_failed payment=%s attempt=%s error=%s",
                payment.id,
                failures,
                exc,
            )
        return payment
    reset_auto_verify_backoff(payment.id)
    return settled


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
    # One way to pay at a time. While the payer holds a live transfer account,
    # the card link stays off the table -- the poll must not quietly hand out a
    # second route to the same money behind the pay button the chat has hidden.
    if bank_transfer_is_current(payment):
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
        PaymentBankTransfer.objects.filter(payment=payment).exclude(
            status=PaymentBankTransfer.Status.PAID
        ).update(status=PaymentBankTransfer.Status.PAID, updated_at=timezone.now())
        record_payment_ledger(payment=payment, event_type="ESCROW_HELD", event_key=f"payment:{payment.id}:held", status_before=old_status, status_after=payment.status, amount_kobo=payment.total_amount_kobo)
        transaction.on_commit(lambda: notify_payment_held(payment))
        if orphaned:
            from notifications.events import notify_support
            transaction.on_commit(lambda payment=payment: notify_support(event_key=f"payment-orphaned:{payment.id}", subject="Paid booking is no longer payable", message=f"Payment {payment.reference} was confirmed by the provider while booking {payment.booking_id} was {payment.booking.status}. The funds are held and need a refund review."), robust=True)
        from conversations.events import ADD_ON_ESCROW_EVENT_CODE, record_booking_event
        if payment.addon_order_id:
            # Add-on escrow belongs to the add-on order, not to the original booking --
            # but the money still lands in this conversation, so say it once, with the
            # add-on's own event code, instead of silently.
            transaction.on_commit(lambda payment=payment: record_booking_event(booking=payment.booking, event_code=ADD_ON_ESCROW_EVENT_CODE, event_key=f"payment:{payment.id}:{ADD_ON_ESCROW_EVENT_CODE.lower()}", metadata={"payment_id": str(payment.id), "addon_order_id": str(payment.addon_order_id)}))
        else:
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
