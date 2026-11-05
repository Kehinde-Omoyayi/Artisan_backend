"""No-code campaign board: audience building, approval and batched dispatch.

Creation is one insert plus one audience estimate (bounded to 20 000 rows of
aggregation). Dispatch always runs in the background, in batches, with a durable
row per recipient so a retry cannot send twice.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core.services import create_audit_log
from home.models import ArtisanProfile, CustomerProfile, CustomUser
from manager.models import (
    Campaign,
    CampaignAudienceSnapshot,
    CampaignDelivery,
    ContactPreference,
)
from manager.services import events as event_service

SEGMENTS = {
    "all_customers": {"label": "All customers", "audience": "customers"},
    "all_artisans": {"label": "All artisans", "audience": "artisans"},
    "verified_artisans": {"label": "Verified artisans", "audience": "artisans"},
    "active_customers": {"label": "Customers with a booking", "audience": "customers"},
    "inactive_customers": {"label": "Customers with no booking for 90 days", "audience": "customers"},
    "new_customers": {"label": "Customers registered in the last 30 days", "audience": "customers"},
    "returning_customers": {"label": "Customers with 2+ bookings", "audience": "customers"},
    "customers_without_bookings": {"label": "Customers who never booked", "audience": "customers"},
    "customers_with_unpaid_booking": {"label": "Customers with an unpaid booking", "audience": "customers"},
    "customers_never_booked": {"label": "Customers who requested but never booked", "audience": "customers"},
    "artisans_in_categories": {"label": "Artisans in selected categories", "audience": "artisans"},
    "artisans_in_locations": {"label": "Artisans in selected states", "audience": "artisans"},
}

CHANNEL_FIELDS = {"EMAIL": "email", "SMS": "phone_number", "IN_APP": None, "PUSH": None}
FREQUENCY_WINDOW_HOURS = 48
BATCH_SIZE = 200


class CampaignError(ValueError):
    pass


def _base_queryset(segment, spec):
    from booking.models import Booking
    from services.models import ServiceRequest

    audience = SEGMENTS[segment]["audience"]
    if audience == "customers":
        qs = CustomUser.objects.filter(is_active=True, customer_profile__isnull=False)
    else:
        qs = CustomUser.objects.filter(is_active=True, artisan_profile__isnull=False)

    if segment == "verified_artisans":
        qs = qs.filter(artisan_profile__verification__verification_status="VERIFIED")
    elif segment == "active_customers":
        qs = qs.filter(id__in=Booking.objects.values("customer__user_id"))
    elif segment == "inactive_customers":
        recent = Booking.objects.filter(created_at__gte=timezone.now() - timedelta(days=90)).values("customer__user_id")
        qs = qs.filter(id__in=Booking.objects.values("customer__user_id")).exclude(id__in=recent)
    elif segment == "new_customers":
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=30))
    elif segment == "returning_customers":
        repeat = Booking.objects.values("customer__user_id").annotate(n=Count("pk")).filter(n__gt=1).values("customer__user_id")
        qs = qs.filter(id__in=repeat)
    elif segment == "customers_without_bookings":
        qs = qs.exclude(id__in=Booking.objects.values("customer__user_id"))
    elif segment == "customers_with_unpaid_booking":
        qs = qs.filter(id__in=Booking.objects.filter(status="REQUESTED").values("customer__user_id"))
    elif segment == "customers_never_booked":
        requested = ServiceRequest.objects.values("requester__user_id")
        qs = qs.filter(id__in=requested).exclude(id__in=Booking.objects.values("customer__user_id"))
    elif segment == "artisans_in_categories":
        categories = [str(value) for value in spec.get("categories") or []][:20]
        if categories:
            qs = qs.filter(artisan_profile__artisan_services__service__category_id__in=categories).distinct()
    elif segment == "artisans_in_locations":
        states = [str(value)[:60] for value in spec.get("states") or []][:20]
        if states:
            qs = qs.filter(artisan_profile__state__in=states)

    if spec.get("require_email"):
        qs = qs.exclude(email="")
    return qs


def estimate(spec):
    """Counts only - no recipient list leaves the backend."""
    segment = str(spec.get("segment") or "all_customers")
    if segment not in SEGMENTS:
        raise CampaignError("Choose an audience segment.")
    channels = [c for c in (spec.get("channels") or ["IN_APP"]) if c in CHANNEL_FIELDS]
    if not channels:
        raise CampaignError("Choose at least one channel.")
    qs = _base_queryset(segment, spec)
    estimated = qs.count()

    opted_out = 0
    invalid = 0
    for channel in channels:
        opted_out += ContactPreference.objects.filter(channel=channel, opted_out=True,
                                                      user_id__in=qs.values("id")).count()
        field = CHANNEL_FIELDS.get(channel)
        if field:
            invalid += qs.filter(**{f"{field}": ""}).count() + qs.filter(**{f"{field}__isnull": True}).count()

    recent_cutoff = timezone.now() - timedelta(hours=FREQUENCY_WINDOW_HOURS)
    recently_contacted = CampaignDelivery.objects.filter(
        user_id__in=qs.values("id"), channel__in=channels, created_at__gte=recent_cutoff
    ).values("user_id").distinct().count()

    excluded = set()
    for channel in channels:
        excluded |= set(ContactPreference.objects.filter(channel=channel, opted_out=True,
                                                         user_id__in=qs.values("id")).values_list("user_id", flat=True))
    excluded |= set(CampaignDelivery.objects.filter(user_id__in=qs.values("id"), channel__in=channels,
                                                    created_at__gte=recent_cutoff).values_list("user_id", flat=True))
    eligible = qs.exclude(id__in=excluded).count()
    return {
        "segment": segment,
        "segment_label": SEGMENTS[segment]["label"],
        "estimated": estimated,
        "eligible": eligible,
        "excluded": len(excluded),
        "opted_out": opted_out,
        "invalid_contact": invalid,
        "recently_contacted": recently_contacted,
        "channels": channels,
        "frequency_window_hours": FREQUENCY_WINDOW_HOURS,
        "note": "Counts are computed on the server. The recipient list itself never leaves the backend.",
    }


def create(*, user, payload):
    title = str(payload.get("title") or "").strip()[:140]
    body = str(payload.get("body") or "").strip()
    if len(title) < 3:
        raise CampaignError("Give the campaign a title of at least 3 characters.")
    if len(body) < 10:
        raise CampaignError("Write a message of at least 10 characters.")
    channels = [channel for channel in (payload.get("channels") or ["IN_APP"]) if channel in CHANNEL_FIELDS]
    if not channels:
        raise CampaignError("Choose at least one channel.")
    segment = payload.get("segment") or "all_customers"
    if segment not in SEGMENTS:
        # The audience builder only offers real segments, but the API is the
        # authority: an unknown one used to travel all the way to the dispatch
        # worker and fail there as a KeyError.
        raise CampaignError(f"Unknown audience segment '{segment}'.")
    spec = {"segment": segment,
            "categories": payload.get("categories") or [],
            "states": payload.get("states") or []}
    counts = estimate({**spec, "channels": channels})
    scheduled_for = payload.get("scheduled_for")
    cta_url = str(payload.get("cta_url") or "").strip()
    if cta_url and not (cta_url.startswith("/") or cta_url.startswith("https://")):
        raise CampaignError("The call-to-action link must be a site path or an https URL.")
    with transaction.atomic():
        campaign = Campaign.objects.create(
            title=title,
            subject=str(payload.get("subject") or "")[:160],
            body=body,
            cta_text=str(payload.get("cta_text") or "")[:60],
            cta_url=cta_url[:300],
            image_url=str(payload.get("image_url") or "")[:300],
            channels=channels,
            audience_spec=spec,
            audience_count=counts["eligible"],
            suppressed_count=counts["excluded"],
            invalid_contact_count=counts["invalid_contact"],
            notes=str(payload.get("notes") or "")[:500],
            requires_approval=True,
            status=Campaign.Status.PENDING_APPROVAL,
            scheduled_for=scheduled_for,
            expires_at=payload.get("expires_at"),
            created_by=user,
        )
        CampaignAudienceSnapshot.objects.create(campaign=campaign, **{
            "estimated": counts["estimated"], "eligible": counts["eligible"], "opted_out": counts["opted_out"],
            "invalid_contact": counts["invalid_contact"], "recently_contacted": counts["recently_contacted"],
            "spec": counts,
        })
        create_audit_log(action="MANAGER_CAMPAIGN_CREATED", resource_type="CAMPAIGN",
                         resource_id=str(campaign.id), actor=user,
                         metadata={"audience": counts["eligible"], "channels": channels})
    return campaign


def approve(*, user, campaign):
    if campaign.status != Campaign.Status.PENDING_APPROVAL:
        raise CampaignError("Only campaigns awaiting approval can be approved.")
    if campaign.created_by_id == user.id:
        raise CampaignError("A campaign must be approved by a different manager.")
    campaign.status = Campaign.Status.SCHEDULED if campaign.scheduled_for else Campaign.Status.SCHEDULED
    campaign.approved_by = user
    campaign.approved_at = timezone.now()
    campaign.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    create_audit_log(action="MANAGER_CAMPAIGN_APPROVED", resource_type="CAMPAIGN",
                     resource_id=str(campaign.id), actor=user, metadata={"title": campaign.title})
    return campaign


def queue_dispatch(*, user, campaign):
    from manager.services import jobs

    if campaign.status not in {Campaign.Status.SCHEDULED, Campaign.Status.DRAFT}:
        raise CampaignError("Only scheduled campaigns can be dispatched.")
    if campaign.status == Campaign.Status.DRAFT:
        raise CampaignError("A draft campaign must be approved before dispatch.")
    job, created = jobs.enqueue(
        kind="CAMPAIGN_DISPATCH",
        payload={"campaign_id": str(campaign.id)},
        user=user,
        dedupe_key=f"campaign:{campaign.id}",
        queue="campaigns",
    )
    Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.Status.DISPATCHING)
    create_audit_log(action="MANAGER_CAMPAIGN_DISPATCHED", resource_type="CAMPAIGN",
                     resource_id=str(campaign.id), actor=user, metadata={"job": str(job.id)})
    return job


def build_recipients(campaign):
    """Create QUEUED delivery rows for eligible recipients, skipping exclusions."""
    spec = {**(campaign.audience_spec or {}), "channels": campaign.channels}
    qs = _base_queryset(spec.get("segment") or "all_customers", spec)
    opted_out_ids = set(
        ContactPreference.objects.filter(channel__in=campaign.channels, opted_out=True)
        .values_list("user_id", flat=True)
    )
    recent_ids = set(
        CampaignDelivery.objects.filter(channel__in=campaign.channels,
                                        created_at__gte=timezone.now() - timedelta(hours=FREQUENCY_WINDOW_HOURS))
        .values_list("user_id", flat=True)
    )
    existing = set(CampaignDelivery.objects.filter(campaign=campaign).values_list("user_id", flat=True))
    created = 0
    skipped = 0
    with transaction.atomic():
        for user in qs.iterator(chunk_size=BATCH_SIZE):
            if user.pk in existing:
                continue
            if user.pk in opted_out_ids or user.pk in recent_ids:
                skipped += 1
                continue
            for channel in campaign.channels:
                if channel in {"EMAIL", "SMS"} and not getattr(user, CHANNEL_FIELDS[channel]):
                    skipped += 1
                    continue
                if CampaignDelivery.objects.filter(campaign=campaign, user=user, channel=channel).exists():
                    continue
                CampaignDelivery.objects.create(campaign=campaign, user=user, channel=channel)
                created += 1
    return {"created": created, "skipped": skipped}


def dispatch(campaign, *, batch_size=BATCH_SIZE):
    """Send one bounded batch. Called repeatedly by the worker until drained."""
    from notifications.in_app import create_in_app_notification

    queued = CampaignDelivery.objects.filter(campaign=campaign, status=CampaignDelivery.Status.QUEUED).select_related(
        "user").order_by("id")[:batch_size]
    sent = failed = 0
    for delivery in queued:
        try:
            if delivery.channel == "IN_APP":
                create_in_app_notification(
                    user=delivery.user,
                    event_key=f"campaign:{campaign.id}:{delivery.user_id}",
                    title=campaign.title,
                    message=campaign.body[:500],
                    data={"campaign": str(campaign.id), "cta": campaign.cta_url, "cta_text": campaign.cta_text},
                )
            elif delivery.channel == "EMAIL":
                from notifications.tasks import send_email_notification

                send_email_notification.delay(
                    event_key=f"campaign:{campaign.id}:{delivery.user_id}",
                    recipient_email=delivery.user.email,
                    recipient_name=delivery.user.get_full_name() or "there",
                    subject=campaign.subject or campaign.title,
                    message=campaign.body,
                    action_url=campaign.cta_url or None,
                    action_label=campaign.cta_text or None,
                )
            elif delivery.channel == "SMS":
                if not campaign_sms_enabled():
                    raise CampaignError("SMS is not enabled on this platform.")
            delivery.status = CampaignDelivery.Status.SENT
            delivery.sent_at = timezone.now()
            delivery.save(update_fields=["status", "sent_at"])
            event_service.record("notification.delivered", entity_type="campaign", entity_id=campaign.pk,
                                 suffix=f"{delivery.pk}:{delivery.channel}")
            sent += 1
        except Exception as exc:
            delivery.status = CampaignDelivery.Status.FAILED
            delivery.error = str(exc)[:240]
            delivery.save(update_fields=["status", "error"])
            failed += 1
    remaining = CampaignDelivery.objects.filter(campaign=campaign, status=CampaignDelivery.Status.QUEUED).count()
    if remaining == 0:
        Campaign.objects.filter(
            pk=campaign.pk,
            status__in=[Campaign.Status.DISPATCHING, Campaign.Status.SCHEDULED],
        ).update(status=Campaign.Status.SENT, sent_at=timezone.now())
        event_service.record("campaign.dispatched", entity_type="campaign", entity_id=campaign.pk, suffix="dispatched")
    return {"sent": sent, "failed": failed, "remaining": remaining}


def campaign_sms_enabled():
    from manager.services.config_engine import current

    return bool(current("notifications.sms_enabled"))


def analytics(campaign):
    deliveries = CampaignDelivery.objects.filter(campaign=campaign)
    counts = {row["status"]: row["n"] for row in deliveries.values("status").annotate(n=Count("pk"))}
    sent = counts.get("SENT", 0) + counts.get("DELIVERED", 0)
    failed = counts.get("FAILED", 0)
    clicked = deliveries.filter(clicked_at__isnull=False).count()
    opened = deliveries.filter(opened_at__isnull=False).count()
    return {
        "id": str(campaign.id),
        "title": campaign.title,
        "status": campaign.status,
        "audience": campaign.audience_count,
        "suppressed": campaign.suppressed_count,
        "queued": counts.get("QUEUED", 0),
        "sent": sent,
        "failed": failed,
        "skipped": counts.get("SKIPPED", 0),
        "opens": opened,
        "open_rate": round(opened * 100 / sent, 1) if sent else 0.0,
        "clicks": clicked,
        "click_rate": round(clicked * 100 / sent, 1) if sent else 0.0,
        "failure_rate": round(failed * 100 / (sent + failed), 1) if (sent + failed) else 0.0,
        "attribution_window_days": campaign.attribution_window_days,
        "suppressed_at_dispatch": int((campaign.dispatch_summary or {}).get("skipped") or 0),
        "last_dispatched_at": (campaign.dispatch_summary or {}).get("at") or "",
    }


def mark_open(*, user, campaign_id):
    """Record that the recipient actually saw the message (in-app inbox read)."""
    delivery = CampaignDelivery.objects.filter(
        campaign_id=campaign_id, user=user, opened_at__isnull=True).order_by("-id").first()
    if not delivery:
        return False
    delivery.opened_at = timezone.now()
    delivery.save(update_fields=["opened_at"])
    return True


def mark_click(*, user, campaign_id):
    """Record a genuine platform return from a campaign link."""
    delivery = CampaignDelivery.objects.filter(campaign_id=campaign_id, user=user).order_by("-id").first()
    if not delivery:
        return False
    if delivery.clicked_at is None:
        delivery.clicked_at = timezone.now()
        delivery.save(update_fields=["clicked_at"])
    return True


def opt_out(*, user, channel, reason=""):
    preference, _ = ContactPreference.objects.update_or_create(
        user=user, channel=channel,
        defaults={"opted_out": True, "reason": reason[:120], "source": "USER"},
    )
    return preference
