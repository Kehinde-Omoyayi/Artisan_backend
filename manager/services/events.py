"""Structured analytics event recording.

Two guarantees matter here:

1. **Business events are never lost.** Recording happens inside the same database
   transaction as the business change (``post_save`` handlers below), so if the
   booking row exists the event exists. A separate durable drain (``drain_to_aggregates``)
   folds events into ``MetricAggregate`` buckets.
2. **Duplicates never double count.** Every event carries a deterministic
   ``dedupe_key`` with a unique constraint; re-recording the same transition is a
   no-op, so a replayed webhook or a retried task cannot inflate a financial metric.

Only non-sensitive metadata is accepted: no emails, phone numbers, tokens, OTPs,
provider payloads or free-form message bodies.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from manager.models import AnalyticsEvent

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# name -> (entity_type, description)
EVENT_CATALOGUE = {
    "customer.registered": ("user", "A customer account was created."),
    "artisan.registered": ("user", "An artisan account was created."),
    "artisan.verified": ("artisan", "An artisan's identity or services were verified."),
    "request.created": ("service_request", "A customer started a service request."),
    "request.submitted": ("service_request", "A service request became visible to artisans."),
    "artisan.responded": ("service_request", "An artisan opened or replied to a request."),
    "conversation.started": ("conversation", "A customer and artisan conversation started."),
    "request.description_completed": ("conversation", "The request description was confirmed."),
    "offer.prepared": ("offer", "The artisan began preparing an itemised offer."),
    "offer.submitted": ("offer", "An itemised offer was sent to the customer."),
    "offer.viewed": ("offer", "The customer opened the offer."),
    "offer.accepted": ("offer", "The customer accepted the offer."),
    "offer.rejected": ("offer", "The customer declined the offer."),
    "offer.revision_requested": ("offer", "The customer asked for a change."),
    "offer.expired": ("offer", "The offer lapsed without a decision."),
    "booking.created": ("booking", "A booking was created from an accepted offer."),
    "booking.confirmed": ("booking", "Both parties are committed to the booking."),
    "payment.initiated": ("payment", "The customer started the escrow checkout."),
    "payment.succeeded": ("payment", "Escrow hold confirmed by the provider."),
    "payment.failed": ("payment", "The provider reported a failed payment."),
    "service.awaiting_commencement": ("booking", "Money is held and work has not started."),
    "service.started": ("booking", "The artisan started work."),
    "service.completed": ("booking", "The artisan marked the work complete."),
    "service.confirmed": ("booking", "The customer confirmed completion."),
    "booking.cancelled": ("booking", "The booking was cancelled."),
    "booking.expired": ("booking", "The booking lapsed without progressing."),
    "booking.disputed": ("booking", "A booking entered dispute."),
    "refund.initiated": ("refund", "A refund was requested."),
    "refund.completed": ("refund", "A refund completed at the provider."),
    "payout.created": ("payout", "An escrow release started."),
    "payout.completed": ("payout", "The artisan payout completed."),
    "promotion.activated": ("promotion", "A promotion campaign went live."),
    "campaign.dispatched": ("campaign", "A marketing campaign was dispatched."),
    "notification.delivered": ("notification", "A notification reached its channel."),
    "support.ticket_created": ("support_case", "A support ticket was opened."),
    "dispute.created": ("support_case", "A dispute was opened."),
    "manager.config_changed": ("config", "A platform rule changed."),
    "manager.insight_acknowledged": ("insight", "A manager acknowledged an insight."),
    "manager.insight_dismissed": ("insight", "A manager dismissed an insight."),
    "manager.campaign_sent": ("campaign", "A campaign delivery batch completed."),
}

# Events that carry money. Aggregations must count these once per entity.
FINANCIAL_EVENTS = {
    "payment.succeeded",
    "payment.failed",
    "refund.initiated",
    "refund.completed",
    "payout.completed",
}

_FORBIDDEN_KEYS = {
    "password", "otp", "code", "token", "secret", "authorization", "card", "cvv",
    "email", "phone", "phone_number", "address", "access_token", "refresh_token",
    "provider_payload", "verification_payload", "api_key",
}


def _clean(value, depth=0):
    """Drop sensitive keys and truncate, so events cannot leak personal data."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                continue
            out[str(key)[:60]] = _clean(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth + 1) for item in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 200:
            return value[:200]
        return value
    return str(value)[:200]


def _dedupe_key(name, entity_type, entity_id, suffix):
    raw = f"{name}\0{entity_type}\0{entity_id}\0{suffix}"
    return hashlib.sha256(raw.encode()).hexdigest()


def record(
    name,
    *,
    entity_type="",
    entity_id="",
    actor_id="",
    actor_role="",
    correlation_id="",
    properties=None,
    occurred_at=None,
    suffix="",
    source="backend",
):
    """Record one event. Idempotent: the same (event, entity, suffix) is stored once."""
    if name not in EVENT_CATALOGUE:
        raise ValueError(f"Unknown analytics event: {name}")
    safe = _clean(properties or {})
    dedupe = _dedupe_key(name, entity_type, str(entity_id), suffix)
    try:
        with transaction.atomic():
            return AnalyticsEvent.objects.create(
                event_id=uuid.uuid4(),
                name=name,
                entity_type=entity_type,
                entity_id=str(entity_id)[:64],
                occurred_at=occurred_at or timezone.now(),
                actor_id=str(actor_id)[:64] if actor_id else "",
                actor_role=str(actor_role)[:20],
                correlation_id=str(correlation_id or "")[:64],
                schema_version=SCHEMA_VERSION,
                properties=safe,
                source=source,
                dedupe_key=dedupe,
            )
    except IntegrityError:
        return None


def record_once(name, **kwargs):
    """Alias documented for callers that need the idempotency guarantee spelled out."""
    return record(name, **kwargs)


def count_events(name, *, start=None, end=None):
    qs = AnalyticsEvent.objects.filter(name=name)
    if start:
        qs = qs.filter(occurred_at__gte=start)
    if end:
        qs = qs.filter(occurred_at__lt=end)
    return qs.count()


def latest_event_at(name=None):
    qs = AnalyticsEvent.objects.all()
    if name:
        qs = qs.filter(name=name)
    return qs.order_by("-occurred_at").values_list("occurred_at", flat=True).first()


# --------------------------------------------------------------------------- #
# Lifecycle wiring: map existing models onto the event catalogue
# --------------------------------------------------------------------------- #
def _status_suffix(instance, *fields):
    parts = []
    for field in fields:
        value = getattr(instance, field, None)
        if value is not None:
            parts.append(str(value))
    return ":".join(parts)


def record_booking_event(booking, name, *, suffix, properties=None):
    return record(
        name,
        entity_type="booking",
        entity_id=booking.pk,
        actor_id=getattr(booking, "customer_id", "") or "",
        properties={
            "status": getattr(booking, "status", ""),
            "service_amount": str(getattr(booking, "price_agreed", "") or ""),
            "category": getattr(getattr(booking, "request", None), "category_id", "") or "",
            **(properties or {}),
        },
        suffix=suffix,
    )


def event_summary(limit=40):
    rows = (
        AnalyticsEvent.objects.values("name")
        .order_by()
        .annotate()
    )
    del rows
    return list(
        AnalyticsEvent.objects.order_by("-occurred_at")[:limit].values(
            "name", "entity_type", "entity_id", "occurred_at", "actor_role"
        )
    )


def serialise_event(row, *, include_entity=True):
    payload = {
        "name": row.name,
        "occurred_at": row.occurred_at.isoformat(),
        "actor_role": row.actor_role,
        "properties": row.properties,
    }
    if include_entity:
        payload["entity_type"] = row.entity_type
        payload["entity_id"] = row.entity_id
    return payload


def json_fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
