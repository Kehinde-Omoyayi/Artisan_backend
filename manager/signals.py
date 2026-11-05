"""Lifecycle → analytics event wiring.

The marketplace already stores its own state; these handlers merely mirror each
real transition into the event store, inside the same transaction. Nothing here
changes business behaviour: no model is modified, no notification is sent, and
every handler swallows its own errors so analytics can never break a booking.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from manager.services.events import record

logger = logging.getLogger(__name__)

# Status → event name maps. A booking that is REQUESTED and unpaid is captured by
# "booking.created"; the payment state adds "payment.*" separately.
BOOKING_EVENTS = {
    "REQUESTED": "booking.created",
    "IN_PROGRESS": "service.started",
    "COMPLETION_REQUESTED": "service.completed",
    "COMPLETED": "service.confirmed",
    "CANCELLED": "booking.cancelled",
    "REJECTED": "booking.cancelled",
    "DISPUTED": "booking.disputed",
}
OFFER_EVENTS = {
    "PROPOSED": "offer.submitted",
    "REVISION_REQUESTED": "offer.revision_requested",
    "COUNTERED": "offer.revision_requested",
    "ACCEPTED": "offer.accepted",
    "DECLINED": "offer.rejected",
    "EXPIRED": "offer.expired",
}
PAYMENT_EVENTS = {
    "HELD": "payment.succeeded",
    "DISPUTED": "payment.succeeded",
    "RELEASE_PENDING": "payment.succeeded",
    "RELEASED": "payment.succeeded",
    "REFUND_PENDING": "payment.succeeded",
    "REFUNDED": "refund.completed",
    "FAILED": "payment.failed",
}
RELEASE_EVENTS = {"SUCCESSFUL": "payout.completed", "FAILED": "payout.completed"}


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # pragma: no cover - analytics must never break a write
            logger.exception("analytics_event_failed handler=%s", fn.__name__)
            return None

    return wrapper


@receiver(post_save, sender="booking.Booking", dispatch_uid="manager_booking_events")
@_safe
def on_booking_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    status = str(getattr(instance, "status", "") or "")
    if created:
        record(
            "booking.created",
            entity_type="booking",
            entity_id=instance.pk,
            actor_id=getattr(instance, "customer_id", "") or "",
            suffix="created",
            properties={"status": status,
                        "agreed_price": str(getattr(instance, "price_agreed", "") or "")},
        )
    name = BOOKING_EVENTS.get(status)
    if not name or (created and name == "booking.created"):
        return
    record(
        name,
        entity_type="booking",
        entity_id=instance.pk,
        actor_id=getattr(instance, "customer_id", "") or "",
        suffix=f"{status}:{'created' if created else 'updated'}",
        properties={
            "status": status,
            "agreed_price": str(getattr(instance, "price_agreed", "") or ""),
            "source": getattr(instance, "source", ""),
            "category_id": getattr(getattr(instance, "request", None), "category_id", "") or "",
        },
    )


@receiver(post_save, sender="conversations.Offer", dispatch_uid="manager_offer_events")
@_safe
def on_offer_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    status = str(getattr(instance, "status", "") or "")
    name = OFFER_EVENTS.get(status)
    if not name:
        return
    actor = "" if created else (getattr(instance, "created_by_id", "") or "")
    record(
        name,
        entity_type="offer",
        entity_id=instance.pk,
        actor_id=actor,
        suffix=f"{status}:v{getattr(instance, 'version', 0)}",
        properties={
            "status": status,
            "version": int(getattr(instance, "version", 0) or 0),
            "total_amount": str(getattr(instance, "total_amount", "") or ""),
            "conversation_type": str(getattr(getattr(instance, "conversation", None), "conversation_type", "") or ""),
        },
    )


@receiver(post_save, sender="payments.EscrowPayment", dispatch_uid="manager_payment_events")
@_safe
def on_payment_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    status = str(getattr(instance, "status", "") or "")
    if created:
        record(
            "payment.initiated",
            entity_type="payment",
            entity_id=instance.pk,
            actor_id=getattr(instance, "customer_id", "") or "",
            suffix=f"initiated:{instance.pk}",
            properties={"total_amount_kobo": int(getattr(instance, "total_amount_kobo", 0) or 0)},
        )
    name = PAYMENT_EVENTS.get(status)
    if not name:
        return
    record(
        name,
        entity_type="payment",
        entity_id=instance.pk,
        actor_id=getattr(instance, "customer_id", "") or "",
        suffix=f"status:{status}",
        properties={
            "status": status,
            "service_amount_kobo": int(getattr(instance, "service_amount_kobo", 0) or 0),
            "total_amount_kobo": int(getattr(instance, "total_amount_kobo", 0) or 0),
            "commission_rate": str(getattr(instance, "commission_rate", "") or ""),
        },
    )


@receiver(post_save, sender="payments.EscrowRelease", dispatch_uid="manager_release_events")
@_safe
def on_release_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    status = str(getattr(instance, "status", "") or "")
    name = RELEASE_EVENTS.get(status)
    if not name:
        return
    record(
        name,
        entity_type="payout",
        entity_id=instance.pk,
        suffix=f"status:{status}",
        properties={"status": status, "amount_kobo": int(getattr(instance, "amount_kobo", 0) or 0)},
    )


@receiver(post_save, sender="conversations.Conversation", dispatch_uid="manager_conversation_events")
@_safe
def on_conversation_saved(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    record(
        "conversation.started",
        entity_type="conversation",
        entity_id=instance.pk,
        actor_id=getattr(instance, "customer_id", "") or "",
        suffix="started",
        properties={"conversation_type": getattr(instance, "conversation_type", "")},
    )


@receiver(post_save, sender="services.ServiceRequest", dispatch_uid="manager_request_events")
@_safe
def on_request_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        record(
            "request.created",
            entity_type="service_request",
            entity_id=instance.pk,
            actor_id=getattr(instance, "requester_id", "") or "",
            suffix="created",
            properties={
                "category_id": getattr(instance, "category_id", "") or "",
                "location_id": getattr(instance, "location_id", "") or "",
                "urgency": getattr(instance, "urgency", ""),
            },
        )
    if str(getattr(instance, "status", "") or "") in {"OPEN", "ATTENDED", "COMPLETED"}:
        record(
            "request.submitted",
            entity_type="service_request",
            entity_id=instance.pk,
            actor_id=getattr(instance, "requester_id", "") or "",
            suffix="submitted",
            properties={"category_id": getattr(instance, "category_id", "") or ""},
        )


@receiver(post_save, sender="support.SupportCase", dispatch_uid="manager_support_events")
@_safe
def on_support_case_saved(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    case_type = str(getattr(instance, "case_type", "") or "")
    record(
        "dispute.created" if case_type == "DISPUTE" else "support.ticket_created",
        entity_type="support_case",
        entity_id=instance.pk,
        actor_id=getattr(instance, "opened_by_id", "") or "",
        suffix="created",
        properties={"case_type": case_type, "priority": getattr(instance, "priority", "")},
    )


@receiver(post_save, sender="support.SupportFinancialAction", dispatch_uid="manager_refund_events")
@_safe
def on_financial_action_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    status = str(getattr(instance, "status", "") or "")
    action = str(getattr(instance, "action_type", "") or "")
    if "REFUND" not in action:
        return
    if created:
        record(
            "refund.initiated",
            entity_type="refund",
            entity_id=instance.pk,
            actor_id=getattr(instance, "requested_by_id", "") or "",
            suffix="initiated",
            properties={"amount_kobo": int(getattr(instance, "amount_kobo", 0) or 0), "action": action},
        )
    if status in {"SUCCEEDED", "FAILED"}:
        record(
            "refund.completed" if status == "SUCCEEDED" else "refund.initiated",
            entity_type="refund",
            entity_id=instance.pk,
            suffix=f"status:{status}",
            properties={"status": status, "amount_kobo": int(getattr(instance, "amount_kobo", 0) or 0)},
        )


@receiver(post_save, sender="reviews.Review", dispatch_uid="manager_review_events")
@_safe
def on_review_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    record(
        "service.confirmed",
        entity_type="booking",
        entity_id=getattr(instance, "booking_id", ""),
        actor_id=getattr(instance, "customer_id", "") or "",
        suffix=f"review:{instance.pk}",
        properties={"rating": int(getattr(instance, "rating", 0) or 0)},
    )


@receiver(post_save, sender="home.CustomUser", dispatch_uid="manager_user_events")
@_safe
def on_user_saved(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    record(
        "customer.registered",
        entity_type="user",
        entity_id=instance.pk,
        actor_id=instance.pk,
        suffix="registered",
        properties={"is_staff": bool(instance.is_staff)},
    )


@receiver(post_save, sender="home.ArtisanProfile", dispatch_uid="manager_artisan_events")
@_safe
def on_artisan_saved(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    record(
        "artisan.registered",
        entity_type="artisan",
        entity_id=instance.pk,
        actor_id=getattr(instance, "user_id", "") or "",
        suffix="registered",
        properties={"profile_status": getattr(instance, "profile_status", "")},
    )


@receiver(post_save, sender="services.ArtisanVerification", dispatch_uid="manager_verification_events")
@_safe
def on_verification_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    status = str(getattr(instance, "verification_status", "") or "")
    if status not in {"VERIFIED", "APPROVED"}:
        return
    record(
        "artisan.verified",
        entity_type="artisan",
        entity_id=getattr(instance, "artisan_id", ""),
        suffix="verified",
        properties={"tier": int(getattr(instance, "verification_tier", 0) or 0)},
    )
