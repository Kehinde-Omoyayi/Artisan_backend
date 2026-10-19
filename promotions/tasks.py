import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.services import create_audit_log
from notifications.events import notify_promotion_campaign_update
from payments.flutterwave import FlutterwaveClient, FlutterwaveError, FlutterwaveUnknownError
from payments.models import PaymentWebhookEvent
from profiles.discovery_cache import invalidate_public_discovery_cache
from .campaign_services import _application_snapshot_is_current, recompute_inventory_schedule
from .models import PromotionApplication, PromotionCampaign, PromotionCampaignAction, PromotionInventory, PromotionPayment
from .maintenance_tasks import purge_performance_dedupe_rows

logger = logging.getLogger(__name__)


class PromotionVerificationPending(Exception):
    pass


def _mark_webhook_processed(event_id):
    if event_id:
        PaymentWebhookEvent.objects.filter(pk=event_id, processed_at__isnull=True).update(processed_at=timezone.now())


def _queue_verification(payment_id, transaction_id=None):
    try:
        verify_promotion_payment.delay(str(payment_id), transaction_id)
    except Exception:
        logger.exception("promotion_payment_verification_enqueue_failed payment=%s", payment_id)


def _record_review_required(payment, campaign, *, reason, result=None):
    before = campaign.status
    was_already_reviewed = (
        payment.status == PromotionPayment.Status.REVIEW_REQUIRED
        and campaign.status == PromotionCampaign.Status.PAYMENT_REVIEW
    )
    payment.status = PromotionPayment.Status.REVIEW_REQUIRED
    payment.failure_reason = reason[:2000]
    if result is not None:
        payment.verification_payload = result
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            transaction_id = str(data.get("id") or "").strip()
            if transaction_id.isascii() and transaction_id.isdigit():
                payment.provider_transaction_id = transaction_id
            payment.provider_status = str(data.get("status") or payment.provider_status)[:60]
            payment.provider_reference = str(data.get("flw_ref") or data.get("reference") or payment.provider_reference)[:150]
    payment.save(update_fields=["status", "failure_reason", "verification_payload", "provider_transaction_id", "provider_status", "provider_reference", "updated_at"])
    campaign.status = PromotionCampaign.Status.PAYMENT_REVIEW
    campaign.last_action_reason = reason[:2000]
    campaign.save(update_fields=["status", "last_action_reason", "updated_at"])
    if not was_already_reviewed:
        from support.models import SupportCase
        from support.services import create_support_case
        case = SupportCase.objects.filter(
            promotion_campaign=campaign,
            status__in=[SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW],
        ).first()
        if case is None:
            case = create_support_case(
                opened_by=campaign.artisan.user,
                case_type=SupportCase.CaseType.PAYMENT,
                subject="Promotion payment verification review",
                description=("This system-generated support case records a promotion payment that could not be safely confirmed. "
                             f"Campaign reference: {campaign.id}; payment reference: {payment.reference}. Review reason: {reason[:1500]}"),
                promotion_campaign=campaign,
                priority="HIGH",
            )
        PromotionCampaignAction.objects.create(
            campaign=campaign,
            action=PromotionCampaignAction.ActionType.PAYMENT_REVIEW,
            status_before=before,
            status_after=campaign.status,
            reason=reason[:2000],
            support_case_id=case.pk,
            metadata={"promotion_payment_id": str(payment.id)},
        )
        create_audit_log(
            action="PROMOTION_PAYMENT_REVIEW_REQUIRED",
            resource_type="PromotionPayment",
            resource_id=str(payment.id),
            actor=None,
            metadata={"campaign_id": str(campaign.id), "reference": payment.reference, "support_case_id": case.pk, "reason": reason[:500]},
        )
        transaction.on_commit(lambda campaign=campaign, note=reason[:1000]: notify_promotion_campaign_update(campaign, action="PAYMENT_REVIEW", reason=note))


def _matching_transaction(result, payment):
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None, "Flutterwave returned no transaction details."
    tx_ref = str(data.get("tx_ref") or "").strip()
    currency = str(data.get("currency") or "").upper()
    transaction_id = str(data.get("id") or "").strip()
    try:
        amount = Decimal(str(data.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        return data, "Flutterwave returned an invalid transaction amount."
    expected = Decimal(payment.amount_kobo) / Decimal(100)
    if tx_ref != payment.reference:
        return data, "Flutterwave transaction reference does not match the promotion payment."
    if currency != payment.currency:
        return data, "Flutterwave transaction currency does not match the promotion payment."
    if amount != expected:
        return data, "Flutterwave transaction amount does not match the manager-configured offer."
    if not transaction_id or not transaction_id.isascii() or not transaction_id.isdigit():
        return data, "Flutterwave returned an invalid transaction identifier."
    return data, ""


@shared_task(bind=True, max_retries=10, default_retry_delay=60)
def initialize_promotion_payment(self, payment_id):
    lock_key = f"promotion-payment-init:{payment_id}"
    try:
        claimed = cache.add(lock_key, "1", timeout=120)
    except Exception:
        claimed = True  # Redis is required in production; DB status and provider reference remain the durable guard.
    if not claimed:
        return {"status": "initialization_already_running"}
    try:
        with transaction.atomic():
            payment = (
                PromotionPayment.objects.select_for_update(of=("self",))
                .select_related("campaign__artisan__user", "campaign__inventory")
                .get(pk=payment_id)
            )
            if payment.status != PromotionPayment.Status.INITIALIZATION_PENDING:
                return {"status": payment.status}
            reference = payment.reference
            amount_ngn = Decimal(payment.amount_kobo) / Decimal(100)
            customer = payment.campaign.artisan.user
            campaign_id = payment.campaign_id
            promotion_type = payment.campaign.promotion_type
            target_key = payment.campaign.inventory.target_key
            target_label = payment.campaign.inventory.target_label
        try:
            result = FlutterwaveClient().initialize_promotion_payment(
                reference=reference,
                amount_ngn=amount_ngn,
                customer=customer,
                promotion_payment_id=payment_id,
                campaign_id=campaign_id,
                promotion_type=promotion_type,
                target_key=target_key,
                target_label=target_label,
            )
        except FlutterwaveUnknownError as exc:
            logger.warning("promotion_payment_init_outcome_unknown payment=%s", payment_id)
            try:
                reconcile_promotion_payment_initialization.delay(str(payment_id))
            except Exception:
                logger.exception("promotion_payment_init_reconcile_enqueue_failed payment=%s", payment_id)
            return {"status": "initialization_outcome_unknown", "detail": str(exc)}
        except FlutterwaveError as exc:
            with transaction.atomic():
                payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
                if payment.status == PromotionPayment.Status.INITIALIZATION_PENDING:
                    payment.status = PromotionPayment.Status.FAILED
                    payment.failure_reason = str(exc)[:2000]
                    payment.save(update_fields=["status", "failure_reason", "updated_at"])
            return {"status": "failed", "detail": str(exc)}

        data = result.get("data") or {}
        authorization_url = str(data.get("link") or data.get("checkout_url") or "").strip()
        if not authorization_url.startswith(("https://", "http://")):
            with transaction.atomic():
                payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
                payment.status = PromotionPayment.Status.REVIEW_REQUIRED
                payment.failure_reason = "Flutterwave initialized the payment without returning a usable authorization URL."
                payment.provider_payload = result
                payment.save(update_fields=["status", "failure_reason", "provider_payload", "updated_at"])
                campaign = PromotionCampaign.objects.select_for_update(of=("self",)).get(pk=payment.campaign_id)
                _record_review_required(payment, campaign, reason=payment.failure_reason, result=result)
            return {"status": "review_required"}
        with transaction.atomic():
            payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
            if payment.status != PromotionPayment.Status.INITIALIZATION_PENDING:
                return {"status": payment.status}
            payment.status = PromotionPayment.Status.INITIALIZED
            payment.authorization_url = authorization_url
            payment.provider_reference = str(data.get("flw_ref") or data.get("reference") or "")[:150]
            payment.provider_status = str(data.get("status") or "initialized")[:60]
            payment.provider_payload = result
            payment.save(update_fields=["status", "authorization_url", "provider_reference", "provider_status", "provider_payload", "updated_at"])
        return {"status": "initialized", "payment_id": str(payment_id)}
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            logger.warning(
                "Could not release promotion payment initialization cache lock; it will expire by TTL.",
                exc_info=True,
            )


@shared_task(bind=True, max_retries=10, default_retry_delay=120)
def reconcile_promotion_payment_initialization(self, payment_id):
    try:
        with transaction.atomic():
            payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
            if payment.status not in {PromotionPayment.Status.INITIALIZATION_PENDING, PromotionPayment.Status.INITIALIZED, PromotionPayment.Status.EXPIRED}:
                return {"status": "not_reconciliation_candidate"}
            reference = payment.reference
            created_at = payment.created_at
            amount_kobo = payment.amount_kobo
        result = FlutterwaveClient().list_transactions(
            tx_ref=reference,
            from_date=created_at.date().isoformat(),
            to_date=timezone.now().date().isoformat(),
        )
        raw = result.get("data") or []
        candidates = raw if isinstance(raw, list) else [raw]
        matched = None
        expected = Decimal(amount_kobo) / Decimal(100)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_reference = str(item.get("tx_ref") or item.get("reference") or "").strip()
            item_currency = str(item.get("currency") or "").upper()
            try:
                item_amount = Decimal(str(item.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            item_id = str(item.get("id") or "").strip()
            if item_reference == reference and item_currency == "NGN" and item_amount == expected and item_id.isascii() and item_id.isdigit():
                matched = item
                break
        if matched:
            with transaction.atomic():
                payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
                if payment.status in {PromotionPayment.Status.VERIFIED, PromotionPayment.Status.REVIEW_REQUIRED, PromotionPayment.Status.REFUNDED}:
                    return {"status": payment.status}
                payment.provider_transaction_id = str(matched["id"])
                payment.provider_reference = str(matched.get("flw_ref") or matched.get("reference") or reference)[:150]
                payment.provider_status = str(matched.get("status") or "")[:60]
                payment.provider_payload = result
                payment.save(update_fields=["provider_transaction_id", "provider_reference", "provider_status", "provider_payload", "updated_at"])
            _queue_verification(payment_id, str(matched["id"]))
            return {"status": "verification_queued", "payment_id": str(payment_id)}
        if payment.expires_at and payment.expires_at <= timezone.now() - timedelta(minutes=15):
            with transaction.atomic():
                payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
                if payment.status in {PromotionPayment.Status.INITIALIZATION_PENDING, PromotionPayment.Status.INITIALIZED} and not payment.provider_transaction_id:
                    payment.status = PromotionPayment.Status.EXPIRED
                    payment.failure_reason = "No matching Flutterwave transaction was found before the checkout expired."
                    payment.save(update_fields=["status", "failure_reason", "updated_at"])
                    return {"status": "expired"}
        raise self.retry(exc=RuntimeError("Flutterwave has not listed this promotion transaction yet."))
    except FlutterwaveUnknownError as exc:
        raise self.retry(exc=exc) from exc
    except PromotionPayment.DoesNotExist:
        return {"status": "not_found"}


@shared_task(bind=True, max_retries=10, default_retry_delay=60)
def verify_promotion_payment(self, payment_id, transaction_id=None, webhook_event_id=None):
    try:
        with transaction.atomic():
            payment = PromotionPayment.objects.select_for_update(of=("self",)).select_related("campaign__artisan__user", "campaign__inventory", "campaign__application").get(pk=payment_id)
            if payment.status in {PromotionPayment.Status.VERIFIED, PromotionPayment.Status.REFUNDED, PromotionPayment.Status.PARTIALLY_REFUNDED}:
                _mark_webhook_processed(webhook_event_id)
                return {"status": payment.status, "payment_id": str(payment.id)}
            if payment.status == PromotionPayment.Status.REVIEW_REQUIRED:
                _mark_webhook_processed(webhook_event_id)
                return {"status": "review_required", "payment_id": str(payment.id)}
            stored_transaction_id = payment.provider_transaction_id
            if stored_transaction_id and transaction_id and str(stored_transaction_id) != str(transaction_id):
                campaign = PromotionCampaign.objects.select_for_update(of=("self",)).get(pk=payment.campaign_id)
                _record_review_required(payment, campaign, reason="Flutterwave callback transaction ID did not match the recorded promotion transaction.")
                _mark_webhook_processed(webhook_event_id)
                return {"status": "review_required", "payment_id": str(payment.id)}
            verify_id = stored_transaction_id or transaction_id
            if not verify_id:
                raise PromotionVerificationPending("Promotion payment has no provider transaction identifier yet.")
            reference = payment.reference
        result = FlutterwaveClient().verify_transaction(verify_id)
        data, mismatch = _matching_transaction(result, payment)
        if mismatch:
            with transaction.atomic():
                payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
                campaign = PromotionCampaign.objects.select_for_update(of=("self",)).get(pk=payment.campaign_id)
                _record_review_required(payment, campaign, reason=mismatch, result=result)
            _mark_webhook_processed(webhook_event_id)
            return {"status": "review_required", "payment_id": str(payment_id)}
        provider_status = str(data.get("status") or "").casefold()
        if provider_status in {"failed", "cancelled", "canceled"}:
            with transaction.atomic():
                payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=payment_id)
                if payment.status not in {PromotionPayment.Status.VERIFIED, PromotionPayment.Status.REFUNDED}:
                    payment.status = PromotionPayment.Status.FAILED
                    payment.provider_transaction_id = str(data.get("id"))
                    payment.provider_reference = str(data.get("flw_ref") or data.get("reference") or reference)[:150]
                    payment.provider_status = provider_status[:60]
                    payment.verification_payload = result
                    payment.failure_reason = str(data.get("processor_response") or data.get("status") or "Flutterwave did not complete this payment.")[:2000]
                    payment.save(update_fields=["status", "provider_transaction_id", "provider_reference", "provider_status", "verification_payload", "failure_reason", "updated_at"])
            _mark_webhook_processed(webhook_event_id)
            return {"status": "failed", "payment_id": str(payment_id)}
        if provider_status not in {"successful", "success"}:
            raise PromotionVerificationPending(f"Flutterwave promotion transaction status is {provider_status or 'pending'}.")

        inventory_id = None
        with transaction.atomic():
            payment = PromotionPayment.objects.select_for_update(of=("self",)).select_related("campaign__application", "campaign__artisan", "campaign__inventory").get(pk=payment_id)
            campaign = PromotionCampaign.objects.select_for_update(of=("self",)).get(pk=payment.campaign_id)
            if payment.status in {PromotionPayment.Status.VERIFIED, PromotionPayment.Status.REFUNDED, PromotionPayment.Status.PARTIALLY_REFUNDED}:
                _mark_webhook_processed(webhook_event_id)
                return {"status": payment.status, "payment_id": str(payment.id)}
            if campaign.verified_payment_id and campaign.verified_payment_id != payment.id:
                _record_review_required(payment, campaign, reason="A second promotion payment succeeded for a campaign that already has a verified payment.", result=result)
                _mark_webhook_processed(webhook_event_id)
                return {"status": "review_required", "payment_id": str(payment.id)}
            try:
                _application_snapshot_is_current(campaign.application)
                if campaign.application_id != campaign.application.pk or campaign.approved_revision_id != campaign.application.latest_revision.pk:
                    raise ValueError("The current approved application revision no longer matches this campaign snapshot.")
            except Exception as exc:
                _record_review_required(payment, campaign, reason=f"Payment was captured but the approved application is no longer eligible: {str(exc)[:1500]}", result=result)
                _mark_webhook_processed(webhook_event_id)
                return {"status": "review_required", "payment_id": str(payment.id)}
            if campaign.status != PromotionCampaign.Status.PENDING_PAYMENT:
                _record_review_required(payment, campaign, reason=f"Payment was captured after the campaign entered {campaign.status}.", result=result)
                _mark_webhook_processed(webhook_event_id)
                return {"status": "review_required", "payment_id": str(payment.id)}
            verified_at = timezone.now()
            payment.status = PromotionPayment.Status.VERIFIED
            payment.provider_transaction_id = str(data.get("id"))
            payment.provider_reference = str(data.get("flw_ref") or data.get("reference") or reference)[:150]
            payment.provider_status = provider_status[:60]
            payment.verification_payload = result
            payment.verified_at = verified_at
            payment.failure_reason = ""
            payment.save(update_fields=["status", "provider_transaction_id", "provider_reference", "provider_status", "verification_payload", "verified_at", "failure_reason", "updated_at"])
            campaign.verified_payment = payment
            campaign.payment_verified_at = verified_at
            campaign.status = PromotionCampaign.Status.QUEUED
            campaign.save(update_fields=["verified_payment", "payment_verified_at", "status", "updated_at"])
            PromotionCampaignAction.objects.create(
                campaign=campaign,
                action=PromotionCampaignAction.ActionType.PAYMENT_VERIFIED,
                status_before=PromotionCampaign.Status.PENDING_PAYMENT,
                status_after=PromotionCampaign.Status.QUEUED,
                actor=None,
                reason="Flutterwave server-side verification matched reference, exact NGN amount, currency and transaction ID.",
                metadata={"promotion_payment_id": str(payment.id), "provider_transaction_id": str(payment.provider_transaction_id)},
            )
            create_audit_log(action="PROMOTION_PAYMENT_VERIFIED", resource_type="PromotionPayment", resource_id=str(payment.id), actor=None, metadata={"campaign_id": str(campaign.id), "reference": payment.reference, "amount_kobo": payment.amount_kobo, "provider_transaction_id": payment.provider_transaction_id})
            inventory_id = campaign.inventory_id
            transaction.on_commit(lambda inventory_id=str(inventory_id): recompute_inventory_schedule(inventory_id))
            transaction.on_commit(invalidate_public_discovery_cache)
        _mark_webhook_processed(webhook_event_id)
        return {"status": "verified", "payment_id": str(payment_id), "campaign_id": str(campaign.id)}
    except PromotionVerificationPending as exc:
        raise self.retry(exc=exc) from exc
    except FlutterwaveUnknownError as exc:
        raise self.retry(exc=exc) from exc
    except FlutterwaveError as exc:
        # Provider errors can be transient or transaction propagation delays; retry, never trust the redirect.
        raise self.retry(exc=exc) from exc
    except PromotionPayment.DoesNotExist:
        _mark_webhook_processed(webhook_event_id)
        return {"status": "not_found", "payment_id": str(payment_id)}


@shared_task
def reconcile_promotion_campaigns():
    """Beat task: execute expiry/queue transitions and pick up stale async payment attempts."""
    now = timezone.now()
    inventory_ids = list(
        PromotionCampaign.objects.filter(
            status__in=[PromotionCampaign.Status.ACTIVE, PromotionCampaign.Status.QUEUED, PromotionCampaign.Status.SCHEDULED],
            payment_verified_at__isnull=False,
        ).values_list("inventory_id", flat=True).distinct()
    )
    updated = 0
    for inventory_id in inventory_ids:
        try:
            recompute_inventory_schedule(inventory_id, now=now)
            updated += 1
        except Exception:
            logger.exception("promotion_inventory_reconciliation_failed inventory=%s", inventory_id)
    stale_payments = PromotionPayment.objects.filter(
        status__in=[PromotionPayment.Status.INITIALIZATION_PENDING, PromotionPayment.Status.INITIALIZED],
        created_at__lte=now - timedelta(minutes=5),
    ).values_list("id", flat=True)[:100]
    for payment_id in stale_payments:
        try:
            reconcile_promotion_payment_initialization.delay(str(payment_id))
        except Exception:
            logger.exception("promotion_payment_reconciliation_enqueue_failed payment=%s", payment_id)
    return {"inventories_checked": updated, "payments_queued": len(stale_payments)}
