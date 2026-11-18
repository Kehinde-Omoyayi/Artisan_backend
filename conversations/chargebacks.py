"""Authenticated provider events add preservation; never infer automatic release.

Unknown/unmatched chargeback events remain pending and globally stop media purge
until reconciled. Financial status is not guessed from a preservation notification.
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from payments.models import EscrowPayment, PaymentWebhookEvent
from .models import MediaPreservationHold


def unresolved_chargebacks():
    return PaymentWebhookEvent.objects.filter(
        provider="flutterwave",
        event_type__startswith="chargeback.",
        processed_at__isnull=True,
    ).exists()


def process_chargeback_event(event):
    data = event.payload.get("data") or {}
    query = Q(pk__in=[])
    if data.get("tx_ref"):
        query |= Q(reference=str(data["tx_ref"]))
    if data.get("transaction_id"):
        query |= Q(provider_transaction_id=str(data["transaction_id"]))
    if data.get("flw_ref"):
        query |= Q(verification_payload__data__flw_ref=str(data["flw_ref"])) | Q(
            provider_reference=str(data["flw_ref"])
        )
    matches = list(EscrowPayment.objects.filter(query).select_related("booking")[:2])
    if len(matches) != 1:
        return False
    payment = matches[0]
    from services.models import ServiceRequest

    with transaction.atomic():
        ServiceRequest.objects.select_for_update().get(pk=payment.booking.request_id)
        # Repeated notifications never resurrect a formally reviewed/released hold.
        MediaPreservationHold.objects.get_or_create(
            provider_reference=f"flutterwave:{data.get('id')}",
            defaults={
                "service_request_id": payment.booking.request_id,
                "kind": "CHARGEBACK",
                "active": True,
            },
        )
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])
    return True


def reconcile_chargeback_events(limit=500):
    from notifications.events import notify_support

    n = 0
    for event in PaymentWebhookEvent.objects.filter(
        provider="flutterwave",
        event_type__startswith="chargeback.",
        processed_at__isnull=True,
    ).order_by("pk")[:limit]:
        if process_chargeback_event(event):
            n += 1
        else:
            notify_support(
                event_key=f"unmatched-chargeback:{event.pk}",
                subject="Unmatched provider chargeback: preservation gate active",
                message=f"Authenticated chargeback event {event.pk} needs payment-reference reconciliation. Recording deletion and payouts remain paused. Review the provider dashboard; do not dismiss without a documented resolution.",
            )
    return n
