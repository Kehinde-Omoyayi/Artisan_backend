import logging
import uuid

from celery import shared_task
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from notifications.events import (
    notify_payment_failed,
    notify_payment_initialized,
    notify_payout_failed,
    notify_payout_pending,
    notify_payout_successful,
)

from .flutterwave import PAYMENT_OPTIONS, FlutterwaveClient, FlutterwaveError, FlutterwaveUnknownError
from .models import EscrowPayment, EscrowRelease
from .services import (
    CHECKOUT_METHODS_MARKER,
    PaymentVerificationPending,
    record_payment_ledger,
    verify_and_hold_payment,
)
from core.services import create_audit_log
from services.identity_names import profile_name_matches_identity
from services.kyc_crypto import decrypt_identity_name
from services.models import ArtisanVerification

logger = logging.getLogger(__name__)


def _mark_webhook_processed(webhook_event_id):
    if not webhook_event_id:
        return
    from .models import PaymentWebhookEvent

    PaymentWebhookEvent.objects.filter(
        pk=webhook_event_id, processed_at__isnull=True
    ).update(processed_at=timezone.now())


# An unknown submit is looked up once, and once only. A queue that retries for
# twenty minutes leaves the payer watching a page that never changes and never
# says why, so the outcome is reported the moment the single retry is spent:
# either the provider confirms the link, or the payment is failed in plain
# words with nothing charged.
RECONCILE_MAX_RETRIES = 1
RECONCILE_RETRY_DELAY_SECONDS = 45


def _abandon_unknown_initialization(payment_id, reason):
    """Stop waiting on a submit the provider cannot confirm, and say so."""
    with transaction.atomic():
        payment = (
            EscrowPayment.objects.select_for_update(of=("self",))
            .select_related("booking", "customer")
            .filter(pk=payment_id)
            .first()
        )
        if payment is None:
            return {"status": "not_found"}
        if (
            payment.provider_transaction_id
            or payment.status != EscrowPayment.Status.PAYMENT_PENDING
        ):
            return {"status": "already_settled", "payment_id": str(payment_id)}
        payment.status = EscrowPayment.Status.FAILED
        payment.failure_reason = (
            "The payment provider did not confirm this payment link. Nothing has "
            "been charged — ask for the link again to try once more."
        )
        payment.save(update_fields=["status", "failure_reason", "updated_at"])
        record_payment_ledger(
            payment=payment,
            event_type="PAYMENT_INITIALIZATION_ABANDONED",
            event_key=f"payment:{payment.id}:initialization-abandoned",
            status_before=EscrowPayment.Status.PAYMENT_PENDING,
            status_after=EscrowPayment.Status.FAILED,
            metadata={"reason": str(reason)[:500]},
        )
        from conversations.events import record_booking_event

        transaction.on_commit(
            lambda payment=payment: record_booking_event(
                booking=payment.booking,
                event_code="PAYMENT_FAILED",
                event_key=f"payment:{payment.id}:initialization_abandoned",
                metadata={"payment_id": str(payment.id), "reason": str(reason)[:200]},
            )
        )
        transaction.on_commit(lambda payment=payment: notify_payment_failed(payment))
        logger.warning(
            "payment_initialization_abandoned payment=%s reason=%s", payment_id, reason
        )
    return {"status": "abandoned_unknown", "payment_id": str(payment_id)}


@shared_task(bind=True, max_retries=RECONCILE_MAX_RETRIES, default_retry_delay=RECONCILE_RETRY_DELAY_SECONDS)
def reconcile_payment_initialization(self, payment_id):
    try:
        with transaction.atomic():
            payment = (
                EscrowPayment.objects.select_for_update(of=("self",))
                .select_related("booking")
                .get(pk=payment_id)
            )
            if (
                payment.provider_transaction_id
                or payment.status != EscrowPayment.Status.PAYMENT_PENDING
            ):
                return {"status": "not_reconciliation_candidate"}
            reference = payment.reference
            created_at = payment.created_at
            expected_amount = payment.total_amount_kobo // 100

        result = FlutterwaveClient().list_transactions(
            tx_ref=reference,
            from_date=created_at.date().isoformat(),
            to_date=timezone.now().date().isoformat(),
        )
        raw = result.get("data") or []
        candidates = raw if isinstance(raw, list) else [raw]
        matched = None
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_reference = str(
                item.get("tx_ref") or item.get("reference") or ""
            ).strip()
            item_currency = str(item.get("currency") or "").upper()
            try:
                item_amount = Decimal(str(item.get("amount")))
            except (TypeError, InvalidOperation, ValueError):
                continue
            item_id = str(item.get("id") or "").strip()
            if (
                item_reference == reference
                and item_currency == "NGN"
                and item_amount == Decimal(expected_amount)
                and item_id
            ):
                matched = item
                break

        if matched is None:
            # Nothing on the provider's side yet. Treated as an unknown outcome
            # so it shares the one-retry rule and the plain-language ending.
            raise FlutterwaveUnknownError(
                "Flutterwave initialization record has not appeared yet."
            )

        with transaction.atomic():
            payment = (
                EscrowPayment.objects.select_for_update(of=("self",))
                .select_related("booking")
                .get(pk=payment_id)
            )
            if (
                payment.provider_transaction_id
                or payment.status != EscrowPayment.Status.PAYMENT_PENDING
            ):
                return {"status": "already_reconciled"}
            payment.provider_transaction_id = str(matched["id"])
            payment.provider_reference = str(
                matched.get("flw_ref") or matched.get("reference") or reference
            )[:150]
            # The checkout marker must survive reconciliation. Overwriting the
            # payload with the provider's transaction list dropped it, which
            # made a perfectly good link look like it was minted for older
            # payment methods -- so the next press of the pay button retired
            # the live reference and handed the payer a fresh link instead.
            previous_payload = (
                payment.provider_payload
                if isinstance(payment.provider_payload, dict)
                else {}
            )
            marker = previous_payload.get(CHECKOUT_METHODS_MARKER)
            marker = marker if isinstance(marker, dict) else {}
            payment.provider_payload = {
                **result,
                CHECKOUT_METHODS_MARKER: {
                    "minted_at": marker.get("minted_at") or timezone.now().isoformat(),
                    "methods": marker.get("methods") or PAYMENT_OPTIONS,
                },
            }
            payment.provider_status = str(matched.get("status") or "initialized")[:50]
            payment.save(
                update_fields=[
                    "provider_transaction_id",
                    "provider_reference",
                    "provider_payload",
                    "provider_status",
                    "updated_at",
                ]
            )
            record_payment_ledger(
                payment=payment,
                event_type="PAYMENT_INITIALIZED_RECONCILED",
                event_key=f"payment:{payment.id}:initialized-reconciled",
                status_before=payment.status,
                status_after=payment.status,
                metadata={"provider_transaction_id": payment.provider_transaction_id},
            )
            from conversations.events import record_booking_event

            transaction.on_commit(
                lambda payment=payment: record_booking_event(
                    booking=payment.booking,
                    event_code="PAYMENT_INITIALIZED",
                    event_key=f"payment:{payment.id}:initialized",
                    metadata={"payment_id": str(payment.id), "reconciled": True},
                )
            )
        verify_flutterwave_payment.delay(
            str(payment.id), str(payment.provider_transaction_id)
        )
        return {"status": "reconciled", "payment_id": str(payment.id)}
    except FlutterwaveUnknownError as exc:
        # One look was already taken. Spending the single allowed retry is the
        # end of it: the payment is failed and the payer is told, rather than
        # left on a page that nothing will ever change.
        if self.request.retries >= RECONCILE_MAX_RETRIES:
            return _abandon_unknown_initialization(payment_id, str(exc))
        raise self.retry(exc=exc) from exc
    except EscrowPayment.DoesNotExist:
        return {"status": "not_found"}


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def verify_flutterwave_payment(
    self, payment_id, transaction_id=None, webhook_event_id=None
):
    try:
        payment = verify_and_hold_payment(
            payment_id,
            provider_transaction_id=transaction_id,
        )
        _mark_webhook_processed(webhook_event_id)
        return {"status": payment.status, "payment_id": str(payment.id)}
    except PaymentVerificationPending as exc:
        raise self.retry(exc=exc) from exc
    except ValueError as exc:
        _mark_webhook_processed(webhook_event_id)
        return {"status": "rejected", "payment_id": str(payment_id), "reason": str(exc)}
    except Exception as exc:
        raise self.retry(exc=exc) from exc


@shared_task(bind=True, max_retries=10, default_retry_delay=120)
def release_artisan_payout(self, payment_id):
    payment_snapshot = EscrowPayment.objects.only("booking_id", "addon_order_id").get(
        pk=payment_id
    )
    with transaction.atomic():
        from booking.models import Booking, BookingAdditionalOrder

        Booking.objects.select_for_update(of=("self",)).get(
            pk=payment_snapshot.booking_id
        )
        if payment_snapshot.addon_order_id:
            BookingAdditionalOrder.objects.select_for_update(of=("self",)).get(
                pk=payment_snapshot.addon_order_id
            )
        payment = (
            EscrowPayment.objects.select_for_update(of=("self",))
            .select_related("booking", "artisan__user", "addon_order")
            .get(pk=payment_id)
        )

        from conversations.chargebacks import unresolved_chargebacks
        from conversations.models import MediaPreservationHold

        if (
            unresolved_chargebacks()
            or MediaPreservationHold.objects.filter(
                service_request_id=payment.booking.request_id,
                kind="CHARGEBACK",
                active=True,
            ).exists()
        ):
            return {"status": "chargeback_preservation_review"}
        if payment.status == EscrowPayment.Status.RELEASED:
            return {"status": "already_released"}
        if payment.status != EscrowPayment.Status.RELEASE_PENDING:
            return {"status": "not_release_pending"}
        if payment.booking.status != "COMPLETED":
            return {"status": "booking_not_completed"}
        if (
            not payment.booking.artisan_completed_at
            or not payment.booking.customer_completed_at
        ):
            return {"status": "completion_requirements_not_met"}
        if payment.artisan.profile_status != "ACTIVE":
            return {"status": "artisan_not_active"}
        verification = ArtisanVerification.objects.filter(
            artisan_id=payment.artisan_id
        ).first()
        if (
            not verification
            or verification.verification_status != "VERIFIED"
            or not verification.verified_identity_name_encrypted
        ):
            return {"status": "artisan_identity_not_verified"}
        try:
            identity_name = decrypt_identity_name(
                verification.verified_identity_name_encrypted
            )
        except ValueError:
            return {"status": "artisan_identity_not_verified"}
        if not profile_name_matches_identity(
            identity_name,
            payment.artisan.user.first_name,
            payment.artisan.user.last_name,
        ):
            return {"status": "artisan_identity_name_mismatch"}

        payout_account = getattr(payment.artisan, "payout_account", None)
        if not payout_account:
            return {"status": "payout_account_missing"}
        if (
            payout_account.status != "VERIFIED"
            or not payout_account.is_active
            or not payout_account.is_verified
        ):
            return {"status": "payout_account_not_verified"}
        if not payout_account.provider_beneficiary_id:
            return {"status": "provider_beneficiary_missing"}

        release_reference = (
            f"RELEASE-{payment.booking_id}"
            if payment.addon_order_id is None
            else f"RELEASE-ADDON-{payment.addon_order_id}"
        )
        release, _ = EscrowRelease.objects.get_or_create(
            payment=payment,
            defaults={
                "reference": release_reference,
                "amount_kobo": payment.artisan_payout_kobo,
            },
        )

        if release.status == EscrowRelease.Status.SUCCESSFUL:
            return {"status": "already_successful"}
        if release.status == EscrowRelease.Status.SUBMITTED:
            transaction.on_commit(
                lambda: reconcile_artisan_payout.delay(str(release.id))
            )
            return {"status": "reconciliation_queued"}
        if release.status == EscrowRelease.Status.FAILED:
            return {"status": "failed_requires_review"}

        release.status = EscrowRelease.Status.SUBMITTED
        release.save(update_fields=["status", "updated_at"])
        record_payment_ledger(
            payment=payment,
            event_type="PAYOUT_INITIATED",
            event_key=f"release:{release.id}:initiated",
            status_before=payment.status,
            status_after=payment.status,
            amount_kobo=release.amount_kobo,
            metadata={"release_id": str(release.id)},
        )
        amount_ngn = Decimal(release.amount_kobo) / Decimal("100")
        beneficiary_id = payout_account.provider_beneficiary_id
        release_reference = release.reference
        transaction.on_commit(lambda: notify_payout_pending(payment))
        from conversations.events import record_booking_event

        transaction.on_commit(
            lambda payment=payment: record_booking_event(
                booking=payment.booking,
                event_code="PAYOUT_INITIATED",
                event_key=f"payment:{payment.id}:payout_initiated",
                metadata={"payment_id": str(payment.id)},
            )
        )

    try:
        result = FlutterwaveClient().create_transfer(
            amount_ngn=amount_ngn,
            beneficiary_id=beneficiary_id,
            reference=release_reference,
            narration=(
                f"Artisan add-on payout {payment.addon_order_id}"
                if payment.addon_order_id
                else f"Artisan payout {payment.booking_id}"
            ),
            callback_url=settings.FLW_TRANSFER_CALLBACK_URL,
        )
    except FlutterwaveUnknownError as exc:
        record_payment_ledger(
            payment=payment,
            event_type="PAYOUT_SUBMISSION_UNKNOWN",
            event_key=f"release:{release.id}:submission-unknown",
            status_before=payment.status,
            status_after=payment.status,
            amount_kobo=release.amount_kobo,
            metadata={"reason": str(exc)[:500]},
        )
        transaction.on_commit(lambda: reconcile_artisan_payout.delay(str(release.id)))
        return {"status": "submission_unknown", "reason": str(exc)}
    except FlutterwaveError as exc:
        with transaction.atomic():
            release = (
                EscrowRelease.objects.select_for_update(of=("self",))
                .select_related("payment__artisan")
                .get(pk=release.id)
            )
            release.status = EscrowRelease.Status.FAILED
            release.failure_reason = str(exc)
            release.save(update_fields=["status", "failure_reason", "updated_at"])
            record_payment_ledger(
                payment=release.payment,
                event_type="PAYOUT_FAILED",
                event_key=f"release:{release.id}:failed",
                status_before=release.payment.status,
                status_after=release.payment.status,
                amount_kobo=release.amount_kobo,
                metadata={"reason": release.failure_reason},
            )
            transaction.on_commit(lambda: notify_payout_failed(release.payment))
            from conversations.events import record_booking_event

            transaction.on_commit(
                lambda payment=release.payment: record_booking_event(
                    booking=payment.booking,
                    event_code="PAYOUT_FAILED",
                    event_key=f"release:{release.id}:failed",
                    metadata={
                        "payment_id": str(payment.id),
                        "release_id": str(release.id),
                    },
                )
            )
        return {"status": "failed", "reason": str(exc)}

    transfer_id = (result.get("data") or {}).get("id")
    with transaction.atomic():
        release = EscrowRelease.objects.select_for_update(of=("self",)).get(
            pk=release.id
        )
        release.provider_transfer_id = str(transfer_id) if transfer_id else None
        release.provider_reference = release_reference
        release.provider_payload = result
        release.status = EscrowRelease.Status.SUBMITTED
        release.save(
            update_fields=[
                "provider_transfer_id",
                "provider_reference",
                "provider_payload",
                "status",
                "updated_at",
            ]
        )
        transaction.on_commit(lambda: reconcile_artisan_payout.delay(str(release.id)))

    return {
        "status": "submitted",
        "transfer_id": str(transfer_id) if transfer_id else None,
    }


@shared_task(bind=True, max_retries=10, default_retry_delay=120)
def reconcile_artisan_payout(self, release_id, webhook_event_id=None):
    with transaction.atomic():
        release = (
            EscrowRelease.objects.select_for_update(of=("self",))
            .select_related("payment__artisan")
            .get(pk=release_id)
        )
        if release.status == EscrowRelease.Status.SUCCESSFUL:
            _mark_webhook_processed(webhook_event_id)
            return {"status": "already_successful"}
        reference = release.reference
        transfer_id = release.provider_transfer_id

    client = FlutterwaveClient()
    try:
        if transfer_id:
            result = client.get_transfer(transfer_id)
        else:
            result = client.get_transfer_by_reference(reference)
    except FlutterwaveUnknownError as exc:
        raise self.retry(exc=exc) from exc
    except FlutterwaveError as exc:
        raise self.retry(exc=exc) from exc

    data = result.get("data") or {}
    if isinstance(data, list):
        exact = [item for item in data if str(item.get("reference", "")) == reference]
        data = exact[0] if exact else {}

    resolved_transfer_id = data.get("id")
    transfer_status = str(data.get("status", "")).upper()
    provider_reference = str(data.get("reference", "")).strip()
    provider_currency = str(data.get("currency", "")).upper()
    expected_amount_ngn = Decimal(release.amount_kobo) / Decimal("100")
    raw_amount = data.get("amount")
    try:
        provider_amount_ngn = Decimal(str(raw_amount))
    except (TypeError, ValueError, InvalidOperation):
        provider_amount_ngn = None

    if not resolved_transfer_id and not transfer_status:
        raise self.retry(
            exc=RuntimeError(
                "Flutterwave transfer could not yet be found by reference."
            )
        )

    if (
        provider_reference != reference
        or provider_currency != "NGN"
        or provider_amount_ngn != expected_amount_ngn
    ):
        raise self.retry(
            exc=RuntimeError(
                "Flutterwave transfer identity or amount does not match the escrow release."
            )
        )

    if transfer_status in {"NEW", "PENDING", "PROCESSING"}:
        raise self.retry(exc=RuntimeError("Flutterwave transfer is still processing."))

    if transfer_status not in {"SUCCESSFUL", "SUCCESS"}:
        with transaction.atomic():
            release = (
                EscrowRelease.objects.select_for_update(of=("self",))
                .select_related("payment__artisan")
                .get(pk=release_id)
            )
            release.provider_transfer_id = (
                str(resolved_transfer_id)
                if resolved_transfer_id
                else release.provider_transfer_id
            )
            release.provider_reference = reference
            release.provider_payload = result
            release.status = EscrowRelease.Status.FAILED
            release.failure_reason = str(
                data.get("complete_message") or "Flutterwave transfer failed."
            )
            release.save(
                update_fields=[
                    "provider_transfer_id",
                    "provider_reference",
                    "provider_payload",
                    "status",
                    "failure_reason",
                    "updated_at",
                ]
            )
            transaction.on_commit(lambda: notify_payout_failed(release.payment))
            from conversations.events import record_booking_event

            transaction.on_commit(
                lambda payment=release.payment: record_booking_event(
                    booking=payment.booking,
                    event_code="PAYOUT_FAILED",
                    event_key=f"release:{release.id}:failed",
                    metadata={
                        "payment_id": str(payment.id),
                        "release_id": str(release.id),
                    },
                )
            )
        _mark_webhook_processed(webhook_event_id)
        return {"status": "failed"}

    with transaction.atomic():
        release = (
            EscrowRelease.objects.select_for_update(of=("self",))
            .select_related("payment__artisan")
            .get(pk=release_id)
        )
        payment = (
            EscrowPayment.objects.select_for_update(of=("self",))
            .select_related("artisan")
            .get(pk=release.payment_id)
        )
        if release.status == EscrowRelease.Status.SUCCESSFUL:
            _mark_webhook_processed(webhook_event_id)
            return {"status": "already_successful"}

        now = timezone.now()
        release.status = EscrowRelease.Status.SUCCESSFUL
        release.completed_at = now
        release.provider_transfer_id = (
            str(resolved_transfer_id)
            if resolved_transfer_id
            else release.provider_transfer_id
        )
        release.provider_reference = reference
        release.provider_payload = result
        release.save(
            update_fields=[
                "status",
                "completed_at",
                "provider_transfer_id",
                "provider_reference",
                "provider_payload",
                "updated_at",
            ]
        )

        old_status = payment.status
        payment.status = EscrowPayment.Status.RELEASED
        payment.released_at = now
        payment.save(update_fields=["status", "released_at", "updated_at"])
        record_payment_ledger(
            payment=payment,
            event_type="PAYOUT_COMPLETED",
            event_key=f"payment:{payment.id}:payout-completed",
            status_before=old_status,
            status_after=payment.status,
            amount_kobo=payment.artisan_payout_kobo,
            metadata={"release_id": str(release.id)},
        )
        transaction.on_commit(lambda: notify_payout_successful(payment))
        from conversations.events import record_booking_event

        transaction.on_commit(
            lambda payment=payment: record_booking_event(
                booking=payment.booking,
                event_code="PAYOUT_COMPLETED",
                event_key=f"release:{release.id}:completed",
                metadata={"payment_id": str(payment.id), "release_id": str(release.id)},
            )
        )

    _mark_webhook_processed(webhook_event_id)
    return {"status": "successful"}


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def initialize_prepared_payment(self, payment_id):
    from .services import _initialize_prepared_payment_external

    lock_key = f"payment:init-lock:{payment_id}"
    token = getattr(self.request, "id", None) or uuid.uuid4().hex
    if not cache.add(lock_key, token, timeout=120):
        return {"status": "already_running"}
    try:
        try:
            payment = EscrowPayment.objects.select_related("customer", "booking").get(
                pk=payment_id
            )
        except EscrowPayment.DoesNotExist:
            return {"status": "not_found"}
        if payment.status != EscrowPayment.Status.PAYMENT_PENDING:
            return {"status": "not_pending"}
        if payment.provider_transaction_id:
            from .services import checkout_link_is_current, rotate_checkout_reference

            if checkout_link_is_current(payment):
                return {"status": "already_initialized"}
            with transaction.atomic():
                payment = rotate_checkout_reference(payment, reason="stale_checkout_link")
        try:
            payment, result = _initialize_prepared_payment_external(payment)
        except FlutterwaveUnknownError:
            reconcile_payment_initialization.delay(str(payment.id))
            return {"status": "submission_unknown", "payment_id": str(payment.id)}
        except FlutterwaveError as exc:
            with transaction.atomic():
                payment = (
                    EscrowPayment.objects.select_for_update(of=("self",))
                    .select_related("booking")
                    .get(pk=payment_id)
                )
                if payment.status == EscrowPayment.Status.PAYMENT_PENDING:
                    payment.status = EscrowPayment.Status.FAILED
                    payment.failure_reason = str(exc)
                    payment.save(
                        update_fields=["status", "failure_reason", "updated_at"]
                    )
                    from conversations.events import record_booking_event

                    transaction.on_commit(
                        lambda payment=payment: record_booking_event(
                            booking=payment.booking,
                            event_code="PAYMENT_FAILED",
                            event_key=f"payment:{payment.id}:initialization_failed",
                            metadata={"payment_id": str(payment.id)},
                        )
                    )
            return {"status": "failed"}
        from conversations.events import record_booking_event

        transaction.on_commit(
            lambda payment=payment: record_booking_event(
                booking=payment.booking,
                event_code="PAYMENT_INITIALIZED",
                event_key=f"payment:{payment.id}:initialized",
                metadata={"payment_id": str(payment.id)},
            )
        )
        transaction.on_commit(
            lambda payment=payment: notify_payment_initialized(payment)
        )
        return {
            "status": "initialized",
            "payment_id": str(payment.id),
            "authorization_url": (result.get("data") or {}).get("link"),
        }
    finally:
        try:
            if cache.get(lock_key) == token:
                cache.delete(lock_key)
        except Exception:
            logger.warning("payment_lock_release_failed", exc_info=True)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def reconcile_stale_payments(self, older_than_minutes=10, batch_size=100):
    cutoff = timezone.now() - __import__("datetime").timedelta(
        minutes=older_than_minutes
    )
    ids = list(
        EscrowPayment.objects.filter(
            status=EscrowPayment.Status.PAYMENT_PENDING,
            provider_transaction_id__isnull=True,
            created_at__lte=cutoff,
        )
        # A payment with a bank-transfer account of any state has its own
        # lifecycle (READY spends down to the provider timer, CREATING is
        # protected by its reservation, OUTCOME_UNKNOWN goes through the
        # unknown-account recovery task, EXPIRED/FAILED stay put). Routing one
        # of those to the hosted-checkout reconciliation could fail a live
        # account that is simply waiting for the money to land.
        .exclude(bank_transfer__isnull=False)
        .order_by("created_at")
        .values_list("id", flat=True)[:batch_size]
    )
    queued = 0
    for payment_id in ids:
        try:
            reconcile_payment_initialization.delay(str(payment_id))
            queued += 1
        except Exception:
            logger.exception(
                "Could not queue stale payment reconciliation: %s", payment_id
            )
    return {"status": "ok", "queued": queued}


@shared_task(max_retries=0)
def auto_verify_ready_transfers(batch_size=25):
    """Scheduled fallback so a paid transfer is found even if nobody is watching.

    The payer's page poll is the normal path to verification, but a browser can
    close and a webhook can be delayed. This sweep looks only at payments whose
    dedicated account is still READY and unexpired, and nudges the same
    verification the poll would — the per-payment cache gate and backoff inside
    ``auto_verify_pending_transfer()`` keep each account to one provider call
    per interval, and nothing is ever marked failed here.
    """
    from payments.models import PaymentBankTransfer
    from .services import auto_verify_pending_transfer

    now = timezone.now()
    limit = max(1, min(int(batch_size or 25), 100))
    ids = list(
        EscrowPayment.objects.filter(
            status=EscrowPayment.Status.PAYMENT_PENDING,
            bank_transfer__status=PaymentBankTransfer.Status.READY,
            bank_transfer__account_expires_at__gt=now,
        )
        .order_by("created_at")
        .values_list("id", flat=True)[:limit]
    )
    checked = 0
    for payment_id in ids:
        payment = (
            EscrowPayment.objects.select_related("booking", "customer", "artisan")
            .filter(pk=payment_id)
            .first()
        )
        if payment is None:
            continue
        try:
            auto_verify_pending_transfer(payment)
            checked += 1
        except Exception:
            logger.exception("auto_verify_ready_transfer_failed payment=%s", payment_id)
    return {"status": "ok", "checked": checked}


@shared_task
def reconcile_financial_ledger(batch_size=500):
    from .ledger import assert_payment_ledger_consistent

    payments = EscrowPayment.objects.prefetch_related("ledger_entries__lines").order_by(
        "created_at"
    )[:batch_size]
    checked = balanced = 0
    failures = []
    for payment in payments:
        checked += 1
        try:
            assert_payment_ledger_consistent(payment)
            balanced += 1
        except ValueError as exc:
            failures.append({"payment_id": str(payment.id), "error": str(exc)})
    if failures:
        logger.error(
            "financial_ledger_inconsistent count=%s sample=%s",
            len(failures),
            failures[:10],
        )
    return {
        "status": "ok" if not failures else "attention_required",
        "checked": checked,
        "balanced": balanced,
        "failures": failures[:25],
    }


@shared_task(bind=True, max_retries=5)
def send_payout_change_email(self, first_name, email, encrypted_code, delivery_id=None):
    """Deliver the one-time code that authorises a payout account change."""
    from home.email_delivery import run_code_email_task
    from home.email_templates import send_security_code_email

    def send(otp_code):
        send_security_code_email(
            recipient_email=email,
            recipient_name=first_name,
            subject="Confirm your payout account change",
            heading="Verify your payout account change",
            intro=(
                "Enter this one-time code in the payout account screen to confirm you want to "
                "replace the saved payout account on your Artisan Market profile."
            ),
            code=otp_code,
            code_label="PAYOUT CHANGE CODE",
            purpose_label="Payout account change",
            security_note=(
                "If you did not request this change, ignore this email and do not share the code. "
                "Your saved payout account will remain unchanged unless the code is entered."
            ),
        )

    return run_code_email_task(
        self, delivery_id=delivery_id, recipient=email,
        encrypted_code=encrypted_code, purpose="payout_change", send=send,
    )


@shared_task(bind=True, max_retries=2, retry_backoff=True, retry_backoff_max=120, retry_jitter=True)
def auto_verify_pending_transfer_task(self, payment_id):
    """Confirm a bank transfer out of the request cycle.

    The payer's page polls the payment record; a GET must never wait on the
    provider, so the actual verification (at most once per payment per backoff
    interval, enforced inside the service) happens here.
    """
    from .services import auto_verify_pending_transfer

    payment = (
        EscrowPayment.objects.filter(pk=payment_id)
        .select_related("booking", "customer", "artisan")
        .first()
    )
    if payment is None:
        return {"status": "not_found"}
    try:
        auto_verify_pending_transfer(payment)
    except Exception:
        logger.exception("auto_verify_pending_transfer_failed payment=%s", payment_id)
        raise
    return {"status": "checked", "payment_id": str(payment_id)}


@shared_task(max_retries=0)
def reconcile_unknown_bank_transfers(batch_size=50):
    """Resolve transfer attempts whose provider outcome was never confirmed.

    Bounded work: at most ``batch_size`` attempts per run, one provider call
    each, always on the original reference. Rows that cannot yet be resolved
    stay OUTCOME_UNKNOWN for the next pass -- or for support.
    """
    from datetime import timedelta

    from payments.models import PaymentBankTransfer
    from .services import heal_unknown_attempt

    cutoff = timezone.now() - timedelta(minutes=2)
    rows = (
        PaymentBankTransfer.objects.filter(
            status=PaymentBankTransfer.Status.OUTCOME_UNKNOWN,
            updated_at__lte=cutoff,
        )
        .select_related("payment", "payment__booking", "payment__customer")
        .order_by("updated_at")[: max(1, min(int(batch_size or 50), 200))]
    )
    resolved = unresolved = failed = 0
    for row in rows:
        try:
            result = heal_unknown_attempt(row.payment, row)
        except Exception:
            logger.exception("bank_transfer_reconcile_failed row=%s", row.pk)
            failed += 1
            continue
        if result is None:
            # The provider confirmed the attempt never existed; it was marked
            # FAILED and can be re-minted fresh.
            resolved += 1
            continue
        state = str(result.get("status") or "")
        if state in {"READY", "PAID", "FAILED"}:
            resolved += 1
        else:
            unresolved += 1
    return {
        "status": "ok",
        "checked": len(rows),
        "resolved": resolved,
        "unresolved": unresolved,
        "failed": failed,
    }
