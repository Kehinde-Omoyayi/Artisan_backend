"""Public promotion placements, first-party performance events and editorial eligibility."""
from datetime import timedelta
import hashlib
import hmac
import re

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from booking.models import Booking
from home.models import ArtisanProfile, ArtisanService
from home.phone_numbers import phone_is_valid
from profiles.discovery_cache import current_discovery_generation, invalidate_public_discovery_cache
from .models import (
    ArtisanMarketPick,
    PromotionApplication,
    PromotionCampaign,
    PromotionCampaignPerformanceDaily,
    PromotionCampaignPerformanceEvent,
    PromotionPayment,
)


PERFORMANCE_TOKEN_SALT = "promotions.performance-event.v1"  # noqa: S105  # nosec B105: public signer namespace, not a credential
VISITOR_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24,180}$")
PLACEMENT_TYPE_BY_CAMPAIGN = {
    PromotionApplication.PromotionType.INSTITUTION: "INSTITUTION_PROMOTION",
    PromotionApplication.PromotionType.AREA: "AREA_PROMOTION",
    PromotionApplication.PromotionType.STATE: "STATE_PROMOTION",
    PromotionApplication.PromotionType.HOMEPAGE: "HOMEPAGE_FEATURE",
}


def make_performance_token(campaign, *, event_type):
    """Sign a short-lived, placement-bound token for a real rendered card or profile visit."""
    if event_type not in PromotionCampaignPerformanceEvent.EventType.values:
        raise ValueError("Unsupported campaign performance event.")
    return signing.dumps(
        {
            "campaign_id": str(campaign.pk),
            "artisan_id": str(campaign.artisan_id),
            "target_key": campaign.inventory.target_key,
            "placement_type": PLACEMENT_TYPE_BY_CAMPAIGN[campaign.promotion_type],
            "event_type": event_type,
        },
        salt=PERFORMANCE_TOKEN_SALT,
        compress=True,
    )


def active_snapshotted_service_ids(campaign):
    """Return the approved service offering IDs that remain active and owned by the artisan."""
    snapshot_ids = {
        item.get("artisan_service_id")
        for item in (campaign.services_snapshot if isinstance(campaign.services_snapshot, list) else [])
        if isinstance(item, dict) and item.get("artisan_service_id") is not None
    }
    if not snapshot_ids:
        return set()
    return set(ArtisanService.objects.filter(
        artisan_id=campaign.artisan_id,
        pk__in=snapshot_ids,
        service__is_active=True,
    ).values_list("pk", flat=True))


def campaign_has_active_snapshot_service(campaign):
    return bool(active_snapshotted_service_ids(campaign))


def campaigns_with_active_snapshot_service(campaigns):
    campaigns = list(campaigns)
    snapshot_ids_by_campaign = {
        campaign.pk: {
            item.get("artisan_service_id")
            for item in (campaign.services_snapshot if isinstance(campaign.services_snapshot, list) else [])
            if isinstance(item, dict) and item.get("artisan_service_id") is not None
        }
        for campaign in campaigns
    }
    all_ids = set().union(*snapshot_ids_by_campaign.values()) if snapshot_ids_by_campaign else set()
    active_pairs = set(ArtisanService.objects.filter(
        pk__in=all_ids,
        service__is_active=True,
    ).values_list("artisan_id", "pk"))
    return [
        campaign for campaign in campaigns
        if any((campaign.artisan_id, service_id) in active_pairs for service_id in snapshot_ids_by_campaign[campaign.pk])
    ]


def eligible_public_active_campaigns(*, now=None, queryset=None):
    """Return campaigns eligible for public display using the canonical public promotion rules."""
    now = now or timezone.now()
    queryset = queryset if queryset is not None else PromotionCampaign.objects.all()
    queryset = queryset.filter(
        status=PromotionCampaign.Status.ACTIVE,
        payment_verified_at__isnull=False,
        started_at__lte=now,
        expires_at__gt=now,
        verified_payment__status=PromotionPayment.Status.VERIFIED,
        artisan__profile_status="ACTIVE",
        artisan__verification__verification_status="VERIFIED",
        artisan__user__is_active=True,
        artisan__user__security_status="NORMAL",
    ).select_related("artisan__user", "artisan__verification", "inventory", "verified_payment")
    return campaigns_with_active_snapshot_service(queryset)


def public_active_campaign_counts_by_inventory(*, now=None, queryset=None):
    """Count currently public campaigns per inventory, including live service eligibility."""
    counts = {}
    for campaign in eligible_public_active_campaigns(now=now, queryset=queryset):
        counts[campaign.inventory_id] = counts.get(campaign.inventory_id, 0) + 1
    return counts


def _current_public_campaign(campaign_id, payload):
    now = timezone.now()
    campaign = (
        PromotionCampaign.objects.select_for_update(of=("self",))
        .select_related("artisan__user", "artisan__verification", "inventory", "verified_payment")
        .filter(
            pk=campaign_id,
            status=PromotionCampaign.Status.ACTIVE,
            payment_verified_at__isnull=False,
            started_at__lte=now,
            expires_at__gt=now,
            verified_payment__status=PromotionPayment.Status.VERIFIED,
            artisan__profile_status="ACTIVE",
            artisan__verification__verification_status="VERIFIED",
            artisan__user__is_active=True,
            artisan__user__security_status="NORMAL",
        )
        .first()
    )
    if not campaign:
        return None
    expected_placement = PLACEMENT_TYPE_BY_CAMPAIGN.get(campaign.promotion_type)
    if (
        payload.get("artisan_id") != str(campaign.artisan_id)
        or payload.get("target_key") != campaign.inventory.target_key
        or payload.get("placement_type") != expected_placement
    ):
        return None
    if not campaign_has_active_snapshot_service(campaign):
        return None
    return campaign


def record_campaign_performance_event(*, token, visitor_id, expected_event_type, expected_artisan_id=None):
    """Record one aggregate event per first-party session and campaign per local day.

    Only a signed API-generated token can identify a paid campaign. A daily HMAC digest is
    persisted instead of raw IP, user agent, user ID or browser identifiers.
    """
    token = str(token or "").strip()
    visitor_id = str(visitor_id or "").strip()
    if not token or len(token) > 4000:
        raise ValidationError({"token": "A valid promotion tracking token is required."})  # nosec B105: user-facing validation text, not a token
    if not VISITOR_KEY_PATTERN.fullmatch(visitor_id):
        raise ValidationError({"visitor_id": "A first-party visitor session key is required."})
    try:
        payload = signing.loads(token, salt=PERFORMANCE_TOKEN_SALT, max_age=60 * 60 * 24)
    except signing.SignatureExpired:
        raise ValidationError({"token": "This promotion tracking link has expired."}) from None  # nosec B105: user-facing validation text, not a token
    except signing.BadSignature:
        raise ValidationError({"token": "This promotion tracking token is invalid."}) from None  # nosec B105: user-facing validation text, not a token
    if not isinstance(payload, dict) or payload.get("event_type") != expected_event_type:
        raise ValidationError({"token": "This token is not valid for the requested performance event."})  # nosec B105: user-facing validation text, not a token
    campaign_id = payload.get("campaign_id")
    try:
        with transaction.atomic():
            campaign = _current_public_campaign(campaign_id, payload)
            if not campaign:
                return False
            if expected_artisan_id is not None and str(campaign.artisan_id) != str(expected_artisan_id):
                return False
            event_day = timezone.localdate()
            visitor_digest = hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                f"promotion-performance-v1:{event_day.isoformat()}:{visitor_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            try:
                with transaction.atomic():
                    PromotionCampaignPerformanceEvent.objects.create(
                        campaign=campaign,
                        event_type=expected_event_type,
                        visitor_digest=visitor_digest,
                        event_day=event_day,
                    )
            except IntegrityError:
                return False
            daily_counter = (
                "impressions"
                if expected_event_type == PromotionCampaignPerformanceEvent.EventType.IMPRESSION
                else "profile_visits"
            )
            daily, _created = PromotionCampaignPerformanceDaily.objects.get_or_create(
                campaign=campaign,
                event_day=event_day,
            )
            PromotionCampaignPerformanceDaily.objects.filter(pk=daily.pk).update(
                **{daily_counter: F(daily_counter) + 1, "updated_at": timezone.now()}
            )
            lifetime_counter = (
                "performance_impressions"
                if expected_event_type == PromotionCampaignPerformanceEvent.EventType.IMPRESSION
                else "performance_profile_visits"
            )
            PromotionCampaign.objects.filter(pk=campaign.pk).update(
                **{lifetime_counter: F(lifetime_counter) + 1}
            )
            return True
    except PromotionCampaign.DoesNotExist:
        return False


def eligible_market_pick_artisans():
    """Return public, verified, complete and service-bearing candidates for editorial curation.

    Portfolio evidence and ratings/service history are surfaced for review when present, but
    are not universal requirements: new artisans can still qualify on their verified,
    complete profile and currently active service work.
    """
    active_services = ArtisanService.objects.filter(
        artisan_id=OuterRef("pk"),
        service__is_active=True,
    )
    queryset = (
        ArtisanProfile.objects.select_related("user", "verification")
        .filter(
            profile_status="ACTIVE",
            user__is_active=True,
            user__security_status="NORMAL",
            verification__verification_status="VERIFIED",
        )
        .exclude(business_name__isnull=True)
        .exclude(business_name__exact="")
        .exclude(bio__isnull=True)
        .exclude(bio__exact="")
        .exclude(user__first_name__exact="")
        .exclude(user__last_name__exact="")
        .exclude(user__phone_number__isnull=True)
        .exclude(user__phone_number__exact="")
        .exclude(country__exact="")
        .exclude(state__exact="")
        .exclude(area__exact="")
        .filter(Exists(active_services))
        .annotate(
            review_count=Count("reviews_received", distinct=True),
            completed_booking_count=Count(
                "booking",
                filter=Q(booking__status=Booking.Status.COMPLETED),
                distinct=True,
            ),
        )
        .order_by("-average_rating", "-completed_booking_count", "business_name", "id")
    )
    # Phone validation and the profile requirements are shared with promotion application eligibility;
    # check it server-side because the catalogue stores phone values as strings.
    return [artisan for artisan in queryset if phone_is_valid(artisan.user.phone_number)]


def invalidate_promotion_selection_cache():
    """Share the public discovery generation for short-lived placement selections."""
    invalidate_public_discovery_cache()


def promotion_selection_generation():
    return current_discovery_generation()


def delete_expired_performance_dedupe_rows(*, now=None):
    """Drop short-lived dedupe digests; aggregate campaign counters remain intact."""
    now = now or timezone.now()
    keep_days = max(int(getattr(settings, "PROMOTION_PERFORMANCE_DEDUPE_DAYS", 2)), 1)
    cutoff = timezone.localdate(now) - timedelta(days=keep_days)
    return PromotionCampaignPerformanceEvent.objects.filter(event_day__lt=cutoff).delete()[0]


def audit_market_pick_update(*, request, pick, action, before=None):
    """Keep editorial curation auditable without exposing internal selection notes publicly."""
    from core.audit import audit_request_action

    audit_request_action(
        request=request,
        action=f"ARTISAN_MARKET_PICK_{action}",
        resource_type="ArtisanMarketPick",
        resource_id=str(pick.id),
        metadata={
            "artisan_id": str(pick.artisan_id),
            "is_active": pick.is_active,
            "position": pick.position,
            "selection_note": pick.selection_note,
            "previous_is_active": before.get("is_active") if before else None,
            "previous_position": before.get("position") if before else None,
            "previous_selection_note": before.get("selection_note") if before else None,
        },
    )
