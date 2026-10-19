"""Promotion offers, canonical inventory, paid queueing and campaign lifecycle."""
import logging
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import unicodedata

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.services import create_audit_log
from home.models import Locations
from notifications.events import notify_promotion_campaign_update
from profiles.discovery_cache import invalidate_public_discovery_cache
from .eligibility import promotion_eligibility
from .models import (
    PromotionApplication,
    PromotionApplicationDecision,
    PromotionCampaign,
    PromotionCampaignAction,
    PromotionInventory,
    PromotionPayment,
    PromotionPlan,
)
from .services import _profile_snapshot, _service_snapshot, _target_snapshot

logger = logging.getLogger(__name__)

QUOTE_SALT = "promotions.checkout-quote.v1"
QUOTE_MAX_AGE_SECONDS = 15 * 60
PAYMENT_EXPIRY_MINUTES = 30


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _target_record(application):
    """Return a canonical, catalogue-backed inventory identity for an approved target."""
    promotion_type = application.promotion_type
    if promotion_type == PromotionApplication.PromotionType.INSTITUTION:
        institution = application.institution
        if not institution or not institution.is_active:
            raise ValidationError({"target": "The approved institution is no longer active in the supported catalogue."})
        label = institution.name + (f" — {institution.campus}" if institution.campus else "") + f" · {institution.location}"
        key = f"INSTITUTION:{institution.pk}"
        return {"promotion_type": promotion_type, "target_key": key, "institution": institution, "area_location": None, "state_country": "", "state_name": "", "label": label}
    if promotion_type == PromotionApplication.PromotionType.AREA:
        location = application.area_location
        if not location:
            raise ValidationError({"target": "The approved area is no longer available."})
        label = str(location)
        return {"promotion_type": promotion_type, "target_key": f"AREA:{location.pk}", "institution": None, "area_location": location, "state_country": "", "state_name": "", "label": label}
    if promotion_type == PromotionApplication.PromotionType.STATE:
        country, state = " ".join(application.state_country.split()), " ".join(application.state_name.split())
        locations = Locations.objects.filter(country__iexact=country, state__iexact=state).order_by("country", "state", "pk")
        canonical = locations.first()
        if not canonical:
            raise ValidationError({"target": "The approved country/state pair is no longer present in the supported location catalogue."})
        country, state = " ".join(canonical.country.split()), " ".join(canonical.state.split())
        key = f"STATE:{_norm(country)}:{_norm(state)}"
        return {"promotion_type": promotion_type, "target_key": key, "institution": None, "area_location": None, "state_country": country, "state_name": state, "label": f"{state}, {country}"}
    return {"promotion_type": PromotionApplication.PromotionType.HOMEPAGE, "target_key": "HOMEPAGE", "institution": None, "area_location": None, "state_country": "", "state_name": "", "label": "Homepage Feature"}


def ensure_homepage_inventory():
    """Ensure the fixed homepage target has a manager-editable persisted capacity row."""
    try:
        inventory, _created = PromotionInventory.objects.get_or_create(
            target_key="HOMEPAGE",
            defaults={
                "promotion_type": PromotionApplication.PromotionType.HOMEPAGE,
                "target_label": "Homepage Feature",
                "capacity": getattr(settings, "PROMOTION_HOMEPAGE_CAPACITY", 3),
            },
        )
    except IntegrityError:
        inventory = PromotionInventory.objects.get(target_key="HOMEPAGE")
    return inventory


def _inventory_for_application(application, *, create=True):
    target = _target_record(application)
    if not create:
        return PromotionInventory.objects.filter(target_key=target["target_key"]).first()
    default_capacity = (
        getattr(settings, "PROMOTION_HOMEPAGE_CAPACITY", 3)
        if target["promotion_type"] == PromotionApplication.PromotionType.HOMEPAGE
        else getattr(settings, "PROMOTION_DEFAULT_TARGET_CAPACITY", 20)
    )
    try:
        inventory, _ = PromotionInventory.objects.get_or_create(
            target_key=target["target_key"],
            defaults={
                "promotion_type": target["promotion_type"],
                "institution": target["institution"],
                "area_location": target["area_location"],
                "state_country": target["state_country"],
                "state_name": target["state_name"],
                "target_label": target["label"],
                "capacity": default_capacity,
            },
        )
    except IntegrityError:
        inventory = PromotionInventory.objects.get(target_key=target["target_key"])
    # Labels are refreshed only for display; capacity and manager settings are never overwritten.
    if inventory.target_label != target["label"]:
        PromotionInventory.objects.filter(pk=inventory.pk).update(target_label=target["label"])
        inventory.target_label = target["label"]
    return inventory


def _application_snapshot_is_current(application):
    """Re-check the exact approved revision, target, offerings and live eligibility before charging."""
    if application.status != PromotionApplication.Status.APPROVED:
        raise ValidationError({"application": "Only an approved application can be purchased."})
    revision = application.latest_revision
    decision = application.decisions.order_by("-created_at", "-id").first()
    if (
        not revision
        or not decision
        or decision.decision_type != PromotionApplicationDecision.DecisionType.APPROVED
        or decision.revision_id != revision.pk
    ):
        raise ValidationError({"application": "The current application revision is not the exact revision approved by Customer Support."})
    snapshot = revision.snapshot if isinstance(revision.snapshot, dict) else {}
    if snapshot.get("promotion_type") != application.promotion_type or snapshot.get("target") != _target_snapshot(application):
        raise ValidationError({"target": "The approved target changed after review. Submit a new application for review."})
    if snapshot.get("promotional_description") != application.promotional_description:
        raise ValidationError({"application": "The approved description changed after review. Submit a new application for review."})
    current_ids = set(application.services.values_list("pk", flat=True))
    snap_ids = {
        item.get("artisan_service_id")
        for item in snapshot.get("services", [])
        if isinstance(item, dict) and item.get("artisan_service_id") is not None
    }
    if current_ids != snap_ids:
        raise ValidationError({"services": "The approved service selection changed after review. Submit a new application for review."})
    offerings = list(application.services.select_related("service__category", "location").order_by("service__name", "pk"))
    if not offerings or any(item.artisan_id != application.artisan_id or not item.service.is_active for item in offerings):
        raise ValidationError({"services": "The approved service offering is no longer active."})
    target = _target_record(application)
    for item in offerings:
        if item.location_id is None or application.promotion_type == PromotionApplication.PromotionType.HOMEPAGE:
            continue
        if application.promotion_type == PromotionApplication.PromotionType.INSTITUTION and item.location_id != application.institution.location_id:
            raise ValidationError({"services": f"{item.service.name} is no longer offered at the exact approved institution location or anywhere."})
        if application.promotion_type == PromotionApplication.PromotionType.AREA and item.location_id != application.area_location_id:
            raise ValidationError({"services": f"{item.service.name} is no longer offered in the exact approved area or anywhere."})
        if application.promotion_type == PromotionApplication.PromotionType.STATE and (
            _norm(item.location.country) != _norm(target["state_country"])
            or _norm(item.location.state) != _norm(target["state_name"])
        ):
            raise ValidationError({"services": f"{item.service.name} is no longer offered in the exact approved state or anywhere."})
    eligibility = promotion_eligibility(application.artisan)
    if not eligibility.get("eligible"):
        raise ValidationError({"eligibility": [item.get("message", "The artisan is no longer eligible.") for item in eligibility.get("requirements", [])]})
    return revision, snapshot, offerings


def _duration_seconds(campaign):
    return int(campaign.remaining_duration_seconds or (campaign.duration_days * 24 * 60 * 60))


def _queue_order(campaign):
    # Verified payment is the normal primary key. A restored campaign gets a fresh
    # re-entry timestamp so it cannot jump ahead of already-paid queue entries.
    return (campaign.queue_reentry_at or campaign.payment_verified_at, str(campaign.id))


def _earliest_slot(intervals, capacity, now):
    if capacity <= 0:
        return None
    candidate = now
    while True:
        blockers = [end for start, end in intervals if start <= candidate < end]
        if len(blockers) < capacity:
            return candidate
        candidate = min(blockers)


def estimate_earliest_start(inventory, *, duration_days, now=None):
    now = now or timezone.now()
    campaigns = list(
        PromotionCampaign.objects.filter(
            inventory=inventory,
            status__in=[PromotionCampaign.Status.ACTIVE, PromotionCampaign.Status.SCHEDULED, PromotionCampaign.Status.QUEUED],
            payment_verified_at__isnull=False,
        ).order_by("payment_verified_at", "id")
    )
    intervals = [
        (campaign.started_at or now, campaign.expires_at)
        for campaign in campaigns
        if campaign.status == PromotionCampaign.Status.ACTIVE and campaign.expires_at and campaign.expires_at > now
    ]
    waiting = sorted(
        [campaign for campaign in campaigns if campaign.status in {PromotionCampaign.Status.QUEUED, PromotionCampaign.Status.SCHEDULED}],
        key=_queue_order,
    )
    for campaign in waiting:
        duration = _duration_seconds(campaign)
        start = _earliest_slot(intervals, inventory.capacity, now)
        if start is None:
            return None
        intervals.append((start, start + timedelta(seconds=duration)))
    return _earliest_slot(intervals, inventory.capacity, now)


def _quote_fingerprint(application, inventory, plan):
    payload = {
        "application": str(application.pk),
        "revision": str(application.latest_revision.pk),
        "promotion_type": application.promotion_type,
        "target_key": inventory.target_key,
        "plan": str(plan.pk),
        "version": plan.version,
        "price_kobo": plan.price_kobo,
        "duration_days": plan.duration_days,
        "terms": plan.terms_text,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_checkout_offer(application, *, now=None):
    revision, _snapshot, _offerings = _application_snapshot_is_current(application)
    try:
        plan = PromotionPlan.objects.get(promotion_type=application.promotion_type)
    except PromotionPlan.DoesNotExist:
        raise ValidationError({"plan": "This promotion product has not been configured by a manager."}) from None
    inventory = _inventory_for_application(application)
    existing_campaign = PromotionCampaign.objects.filter(application=application).first()
    if existing_campaign:
        if existing_campaign.status != PromotionCampaign.Status.PENDING_PAYMENT or existing_campaign.payment_verified_at:
            raise ValidationError({"campaign": "This approved application already has a paid or closed campaign. Review it in My Promotions."})
        # Once the first attempt was initialized, its accepted price/terms are immutable for retry.
        if existing_campaign.payments.exists():
            plan_name = existing_campaign.plan_name_snapshot
            price_kobo = existing_campaign.price_kobo
            duration_days = existing_campaign.duration_days
            terms_text = existing_campaign.terms_snapshot
            terms_version = existing_campaign.accepted_terms_version
            available = True
        else:
            plan_name, price_kobo, duration_days = plan.name, plan.price_kobo, plan.duration_days
            terms_text, terms_version = plan.terms_text, plan.version
            available = bool(plan.is_enabled and plan.price_kobo > 0 and plan.terms_text.strip())
    else:
        plan_name, price_kobo, duration_days = plan.name, plan.price_kobo, plan.duration_days
        terms_text, terms_version = plan.terms_text, plan.version
        available = bool(plan.is_enabled and plan.price_kobo > 0 and plan.terms_text.strip())
    now = now or timezone.now()
    earliest = estimate_earliest_start(inventory, duration_days=duration_days, now=now)
    token_data = {
        "application_id": str(application.pk),
        "revision_id": str(revision.pk),
        "target_key": inventory.target_key,
        "plan_id": str(plan.pk),
        "plan_version": plan.version,
        "price_kobo": price_kobo,
        "duration_days": duration_days,
        "plan_name": plan_name,
        "terms_text": terms_text,
        "terms_version": terms_version,
        "fingerprint": _quote_fingerprint(application, inventory, plan),
        "issued_at": now.isoformat(),
    }
    token = signing.dumps(token_data, salt=QUOTE_SALT, compress=True)
    paid_count = PromotionCampaign.objects.filter(
        inventory=inventory,
        status__in=[PromotionCampaign.Status.ACTIVE, PromotionCampaign.Status.SCHEDULED, PromotionCampaign.Status.QUEUED],
        payment_verified_at__isnull=False,
    ).count()
    if inventory.capacity == 0:
        availability_reason = "This placement is temporarily unavailable because its manager-configured capacity is zero."
    elif not available:
        availability_reason = "This plan is not currently available. Homepage Feature remains unavailable until an authorized manager configures and enables it."
    else:
        availability_reason = ""
    return {
        "application_id": str(application.pk),
        "promotion_type": application.promotion_type,
        "promotion_type_label": application.get_promotion_type_display(),
        "target": {**_target_record(application), "institution": str(application.institution_id) if application.institution_id else None, "area_location": str(application.area_location_id) if application.area_location_id else None},
        "plan_name": plan_name,
        "price": str(Decimal(price_kobo) / Decimal(100)),
        "price_kobo": price_kobo,
        "currency": "NGN",
        "duration_days": duration_days,
        "terms_text": terms_text,
        "terms_version": terms_version,
        "terms_changed": bool(existing_campaign and existing_campaign.payments.exists()),
        "availability": "AVAILABLE" if available and inventory.capacity > 0 else "UNAVAILABLE",
        "availability_reason": availability_reason,
        "capacity": inventory.capacity,
        "paid_campaigns_in_inventory": paid_count,
        "earliest_start_at": earliest.isoformat() if earliest else None,
        "estimated_wait": "NOW" if earliest and earliest <= now else ("WAITING" if earliest else "NO_CAPACITY_SCHEDULED"),
        "schedule_note": "The final queue position is assigned only after Flutterwave verification. Another earlier verified payment or a manager capacity change may move this estimate.",
        "quote_token": token,
        "quote_expires_in_seconds": QUOTE_MAX_AGE_SECONDS,
    }


def _target_label_from_snapshot(snapshot):
    target = snapshot.get("target") or {}
    if target.get("kind") == "INSTITUTION":
        return " · ".join(filter(None, [target.get("name"), target.get("campus"), (target.get("location") or {}).get("area"), (target.get("location") or {}).get("state")]))
    if target.get("kind") == "AREA":
        loc = target
        return ", ".join(filter(None, [loc.get("area"), loc.get("state"), loc.get("country")]))
    if target.get("kind") == "STATE":
        return f"{target.get('state', '')}, {target.get('country', '')}".strip(", ")
    return "Homepage Feature"


def start_campaign_checkout(application, *, user, quote_token, accepted_terms, accepted_terms_version, now=None):
    if not accepted_terms:
        raise ValidationError({"accepted_terms": "You must accept the configured promotion terms before payment."})
    try:
        quote = signing.loads(quote_token, salt=QUOTE_SALT, max_age=QUOTE_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise ValidationError({"quote_token": "This offer expired. Refresh the page to review the current price and schedule."}) from None  # nosec B105: user-facing validation text, not a token
    except signing.BadSignature:
        raise ValidationError({"quote_token": "This promotion offer is invalid. Refresh the page and try again."}) from None  # nosec B105: user-facing validation text, not a token
    revision, snapshot, offerings = _application_snapshot_is_current(application)
    inventory = _inventory_for_application(application)
    plan = PromotionPlan.objects.filter(promotion_type=application.promotion_type).first()
    if not plan:
        raise ValidationError({"plan": "This plan is not configured."})
    if (
        quote.get("application_id") != str(application.pk)
        or quote.get("revision_id") != str(revision.pk)
        or quote.get("target_key") != inventory.target_key
        or quote.get("plan_id") != str(plan.pk)
        or quote.get("fingerprint") != _quote_fingerprint(application, inventory, plan)
        or int(quote.get("plan_version", -1)) != plan.version
    ):
        raise ValidationError({"quote_token": "The approved application or offer changed. Review the current offer before proceeding."})  # nosec B105: user-facing validation text, not a token
    if accepted_terms_version != quote.get("terms_version"):
        raise ValidationError({"accepted_terms_version": "The accepted terms version does not match this offer."})
    if quote.get("terms_version") == plan.version and (not plan.is_enabled or plan.price_kobo <= 0 or not plan.terms_text.strip()):
        raise ValidationError({"plan": "This plan is not currently available."})
    now = now or timezone.now()
    with transaction.atomic():
        locked_application = PromotionApplication.objects.select_for_update(of=("self",)).select_related("artisan__user", "institution__location", "area_location").get(pk=application.pk)
        plan = PromotionPlan.objects.select_for_update(of=("self",)).get(pk=plan.pk)
        # Repeat offer, eligibility and snapshot checks after obtaining row locks.
        if (
            quote.get("application_id") != str(locked_application.pk)
            or quote.get("revision_id") != str(locked_application.latest_revision.pk)
            or quote.get("target_key") != _inventory_for_application(locked_application).target_key
            or quote.get("plan_id") != str(plan.pk)
            or quote.get("fingerprint") != _quote_fingerprint(locked_application, _inventory_for_application(locked_application), plan)
            or int(quote.get("plan_version", -1)) != plan.version
        ):
            raise ValidationError({"quote_token": "The approved application or plan changed while you were checking out. Refresh the offer and accept the current terms."})  # nosec B105: user-facing validation text, not a token
        if int(quote.get("terms_version", -1)) == plan.version and (not plan.is_enabled or plan.price_kobo <= 0 or not plan.terms_text.strip()):
            raise ValidationError({"plan": "This plan is no longer available."})
        revision, snapshot, offerings = _application_snapshot_is_current(locked_application)
        inventory = _inventory_for_application(locked_application)
        inventory = PromotionInventory.objects.select_for_update(of=("self",)).get(pk=inventory.pk)
        if inventory.capacity <= 0:
            raise ValidationError({"availability": "This target currently has no capacity for new campaigns."})
        campaign = PromotionCampaign.objects.select_for_update(of=("self",)).filter(application=locked_application).first()
        if campaign and (campaign.payment_verified_at or campaign.status != PromotionCampaign.Status.PENDING_PAYMENT):
            raise ValidationError({"campaign": "This approved application already has a paid or closed campaign. Review My Promotions."})
        if campaign is None:
            _target_record(locked_application)  # Validate that the canonical target record still exists.
            target_snapshot = dict(snapshot.get("target") or {})
            target_snapshot["label"] = _target_label_from_snapshot(snapshot)
            campaign = PromotionCampaign.objects.create(
                application=locked_application,
                approved_revision=revision,
                artisan=locked_application.artisan,
                inventory=inventory,
                plan=plan,
                promotion_type=locked_application.promotion_type,
                target_snapshot=target_snapshot,
                description_snapshot=locked_application.promotional_description,
                services_snapshot=_service_snapshot(offerings),
                profile_snapshot=_profile_snapshot(locked_application.artisan),
                plan_name_snapshot=quote["plan_name"],
                price_kobo=int(quote["price_kobo"]),
                currency="NGN",
                duration_days=int(quote["duration_days"]),
                terms_snapshot=quote["terms_text"],
                refund_policy_snapshot=plan.refund_policy,
                accepted_terms_version=int(quote["terms_version"]),
                terms_accepted_at=now,
                terms_accepted_by=user,
                status=PromotionCampaign.Status.PENDING_PAYMENT,
            )
        pending = campaign.payments.filter(status__in=[PromotionPayment.Status.INITIALIZATION_PENDING, PromotionPayment.Status.INITIALIZED, PromotionPayment.Status.VERIFYING, PromotionPayment.Status.REVIEW_REQUIRED]).order_by("-created_at").first()
        if pending:
            return campaign, pending, False
        reference = f"PROMO-{campaign.id.hex[:16].upper()}-{timezone.now().strftime('%y%m%d%H%M%S')}-{PromotionPayment.objects.filter(campaign=campaign).count() + 1}"
        payment = PromotionPayment.objects.create(
            campaign=campaign,
            reference=reference,
            amount_kobo=campaign.price_kobo,
            currency=campaign.currency,
            expires_at=now + timedelta(minutes=PAYMENT_EXPIRY_MINUTES),
        )
        create_audit_log(action="PROMOTION_PAYMENT_INITIALIZATION_QUEUED", resource_type="PromotionPayment", resource_id=str(payment.id), actor=user, metadata={"campaign_id": str(campaign.id), "application_id": str(locked_application.id), "reference": payment.reference, "amount_kobo": payment.amount_kobo})
        return campaign, payment, True


def _campaign_action(campaign, *, action, actor=None, reason="", evidence_summary="", support_case_id=None, status_before="", status_after="", refund_action_id=None, metadata=None):
    return PromotionCampaignAction.objects.create(
        campaign=campaign,
        action=action,
        status_before=status_before,
        status_after=status_after,
        reason=reason,
        evidence_summary=evidence_summary,
        support_case_id=support_case_id,
        actor=actor,
        actor_name_snapshot=(
            ((actor.get_full_name() or getattr(actor, "email", ""))[:160])
            if actor is not None else ""
        ),
        refund_action_id=refund_action_id,
        metadata=metadata or {},
    )


def _notify_campaign(campaign, action, reason=""):
    notify_promotion_campaign_update(campaign, action=action, reason=reason)


def recompute_inventory_schedule(inventory_id, *, now=None, actor=None, correction_reason=""):
    """Lock one canonical inventory row, expire slots, and rebuild its paid queue idempotently."""
    now = now or timezone.now()
    with transaction.atomic():
        inventory = PromotionInventory.objects.select_for_update(of=("self",)).get(pk=inventory_id)
        campaigns = list(
            PromotionCampaign.objects.select_for_update(of=("self",))
            .filter(inventory=inventory)
            .select_related("verified_payment")
            .order_by("created_at", "id")
        )
        public_before = {
            str(campaign.pk) for campaign in campaigns
            if campaign.status == PromotionCampaign.Status.ACTIVE and campaign.started_at and campaign.started_at <= now and campaign.expires_at and campaign.expires_at > now
        }
        active = [campaign for campaign in campaigns if campaign.status == PromotionCampaign.Status.ACTIVE]
        for campaign in active:
            if campaign.expires_at and campaign.expires_at <= now:
                before = campaign.status
                campaign.status = PromotionCampaign.Status.EXPIRED
                campaign.ended_at = campaign.expires_at
                campaign.remaining_duration_seconds = 0
                campaign.save(update_fields=["status", "ended_at", "remaining_duration_seconds", "updated_at"])
                _campaign_action(campaign, action=PromotionCampaignAction.ActionType.EXPIRED, status_before=before, status_after=campaign.status, reason="The paid promotion term expired.")
                _notify_campaign(campaign, PromotionCampaignAction.ActionType.EXPIRED, "The paid promotion term ended.")
        active = [campaign for campaign in campaigns if campaign.status == PromotionCampaign.Status.ACTIVE and campaign.expires_at and campaign.expires_at > now]
        intervals = [(campaign.started_at or now, campaign.expires_at, campaign) for campaign in active]
        waiting = sorted(
            [campaign for campaign in campaigns if campaign.status in {PromotionCampaign.Status.QUEUED, PromotionCampaign.Status.SCHEDULED} and campaign.payment_verified_at],
            key=_queue_order,
        )
        for campaign in waiting:
            duration = _duration_seconds(campaign)
            old_status = campaign.status
            old_start = campaign.scheduled_start_at
            old_expiry = campaign.expires_at
            start = _earliest_slot([(s, e) for s, e, _ in intervals], inventory.capacity, now)
            if start is None:
                campaign.status = PromotionCampaign.Status.QUEUED
                campaign.scheduled_start_at = None
                campaign.expires_at = None
                campaign.remaining_duration_seconds = duration
                campaign.save(update_fields=["status", "scheduled_start_at", "expires_at", "remaining_duration_seconds", "updated_at"])
                if old_status != campaign.status or old_start is not None or old_expiry is not None:
                    _campaign_action(
                        campaign,
                        action=PromotionCampaignAction.ActionType.SCHEDULED,
                        actor=actor,
                        status_before=old_status,
                        status_after=campaign.status,
                        reason=correction_reason or "The paid campaign returned to the verified-payment queue because target capacity was unavailable.",
                        metadata={"scheduled_start_at": None, "scheduled_end_at": None, "capacity_recalculation": bool(correction_reason)},
                    )
                continue
            end = start + timedelta(seconds=duration)
            if start <= now:
                campaign.status = PromotionCampaign.Status.ACTIVE
                campaign.started_at = now
                campaign.scheduled_start_at = None
                campaign.expires_at = now + timedelta(seconds=duration)
                campaign.remaining_duration_seconds = None
                campaign.save(update_fields=["status", "started_at", "scheduled_start_at", "expires_at", "remaining_duration_seconds", "updated_at"])
                intervals.append((now, campaign.expires_at, campaign))
                if old_status != PromotionCampaign.Status.ACTIVE:
                    _campaign_action(
                        campaign,
                        action=PromotionCampaignAction.ActionType.STARTED,
                        actor=actor,
                        status_before=old_status,
                        status_after=campaign.status,
                        reason=correction_reason or "The paid campaign entered an available target slot.",
                        metadata={"capacity_recalculation": bool(correction_reason)},
                    )
                    _notify_campaign(campaign, PromotionCampaignAction.ActionType.STARTED, "Your paid promotion is now live.")
            else:
                campaign.status = PromotionCampaign.Status.SCHEDULED
                campaign.scheduled_start_at = start
                campaign.expires_at = end
                campaign.remaining_duration_seconds = duration
                campaign.save(update_fields=["status", "scheduled_start_at", "expires_at", "remaining_duration_seconds", "updated_at"])
                intervals.append((start, end, campaign))
                if old_status != campaign.status or old_start != start or old_expiry != end:
                    _campaign_action(
                        campaign,
                        action=PromotionCampaignAction.ActionType.SCHEDULED,
                        actor=actor,
                        status_before=old_status,
                        status_after=campaign.status,
                        reason=correction_reason or "A paid target slot was reserved by verified payment order.",
                        metadata={
                            "scheduled_start_at": start.isoformat(),
                            "scheduled_end_at": end.isoformat(),
                            "capacity_recalculation": bool(correction_reason),
                        },
                    )
                    _notify_campaign(campaign, PromotionCampaignAction.ActionType.SCHEDULED, f"Your paid campaign is scheduled to begin {start.isoformat()}.")
        public_after = {
            str(campaign.pk) for campaign in campaigns
            if campaign.status == PromotionCampaign.Status.ACTIVE and campaign.started_at and campaign.started_at <= now and campaign.expires_at and campaign.expires_at > now
        }
        if public_before != public_after:
            transaction.on_commit(invalidate_public_discovery_cache)
        return {"inventory_id": str(inventory.pk), "active": len([item for item in intervals if item[0] <= now < item[1]]), "scheduled": sum(1 for item in campaigns if item.status == PromotionCampaign.Status.SCHEDULED), "queued": sum(1 for item in campaigns if item.status == PromotionCampaign.Status.QUEUED)}


def pause_or_restore_or_revoke_campaign(campaign_id, *, actor, action, reason, evidence_summary="", support_case_id=None, confirmed_violation=False):
    from datetime import datetime
    from support.models import SupportCase
    from support.permissions import SUPPORT_SUPERVISOR, SUPPORT_ADMIN, SUPPORT_AGENT, user_has_support_role

    allowed = {"PAUSE", "SUSPEND", "RESTORE", "REVOKE"}
    if action not in allowed:
        raise ValidationError({"action": "Choose PAUSE, SUSPEND, RESTORE or REVOKE."})
    if not user_has_support_role(actor, {SUPPORT_AGENT, SUPPORT_SUPERVISOR, SUPPORT_ADMIN}):
        raise ValidationError({"permission": "Authorized Customer Support access is required."})
    if action in {"RESTORE", "REVOKE"} and not user_has_support_role(actor, {SUPPORT_SUPERVISOR, SUPPORT_ADMIN}):
        raise ValidationError({"permission": "Customer Support supervisor approval is required for restoration or revocation."})
    if not reason or len(reason.strip()) < 10:
        raise ValidationError({"reason": "Enter a reason of at least 10 characters."})
    if not support_case_id:
        raise ValidationError({"support_case_id": "Link campaign actions to the reviewed support case."})
    case = SupportCase.objects.select_related("promotion_campaign").filter(pk=support_case_id).first()
    if not case or case.promotion_campaign_id != campaign_id:
        raise ValidationError({"support_case_id": "Select a support case linked to this exact campaign."})
    if case.status not in {SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW}:
        raise ValidationError({"support_case_id": "Campaign actions require an open or under-review support case."})
    if action == "REVOKE" and confirmed_violation and len(evidence_summary.strip()) < 20:
        raise ValidationError({"evidence_summary": "Summarize the reviewed evidence (at least 20 characters); a complaint by itself is not proof."})
    if action == "REVOKE" and confirmed_violation and case.status != SupportCase.Status.UNDER_REVIEW:
        raise ValidationError({"support_case_id": "A confirmed violation requires a support case that has been formally reviewed."})

    now = timezone.now()
    with transaction.atomic():
        campaign = PromotionCampaign.objects.select_for_update(of=("self",)).select_related("artisan__user", "inventory", "verified_payment").get(pk=campaign_id)
        inventory = PromotionInventory.objects.select_for_update(of=("self",)).get(pk=campaign.inventory_id)
        # Re-read the linked case under lock so a concurrent resolution/closure
        # cannot race a moderation action after the earlier validation.
        case = SupportCase.objects.select_for_update(of=("self",)).filter(pk=support_case_id).first()
        if not case or case.promotion_campaign_id != campaign_id:
            raise ValidationError({"support_case_id": "Select a support case linked to this exact campaign."})
        if case.status not in {SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW}:
            raise ValidationError({"support_case_id": "Campaign actions require an open or under-review support case."})
        before = campaign.status
        if action in {"PAUSE", "SUSPEND"}:
            if before not in {PromotionCampaign.Status.ACTIVE, PromotionCampaign.Status.SCHEDULED, PromotionCampaign.Status.QUEUED}:
                raise ValidationError({"campaign": "Only a paid active, scheduled or queued campaign can be paused or suspended."})
            if before == PromotionCampaign.Status.ACTIVE and campaign.expires_at:
                remaining = max(int((campaign.expires_at - now).total_seconds()), 0)
            elif before == PromotionCampaign.Status.SCHEDULED and campaign.scheduled_start_at and campaign.scheduled_start_at > now:
                remaining = campaign.duration_days * 86400
            else:
                remaining = _duration_seconds(campaign)
            campaign.remaining_duration_seconds = remaining
            campaign.status = PromotionCampaign.Status.PAUSED if action == "PAUSE" else PromotionCampaign.Status.SUSPENDED
            campaign.paused_at = now
            campaign.scheduled_start_at = None
            campaign.started_at = None
            campaign.expires_at = None
            campaign.last_action_reason = reason.strip()
            campaign.save(update_fields=["remaining_duration_seconds", "status", "paused_at", "scheduled_start_at", "started_at", "expires_at", "last_action_reason", "updated_at"])
            event_action = PromotionCampaignAction.ActionType.PAUSED if action == "PAUSE" else PromotionCampaignAction.ActionType.SUSPENDED
        elif action == "RESTORE":
            if before not in {PromotionCampaign.Status.PAUSED, PromotionCampaign.Status.SUSPENDED} or not campaign.payment_verified_at or not campaign.verified_payment_id:
                raise ValidationError({"campaign": "Only a paid paused or suspended campaign can be restored."})
            eligibility = promotion_eligibility(campaign.artisan)
            if not eligibility.get("eligible"):
                raise ValidationError({"eligibility": [item.get("message", "The artisan is not currently eligible.") for item in eligibility.get("requirements", [])]})
            if campaign.application.status != PromotionApplication.Status.APPROVED:
                raise ValidationError({"application": "The underlying approved application is no longer valid."})
            campaign.status = PromotionCampaign.Status.QUEUED
            campaign.queue_reentry_at = now
            campaign.paused_at = None
            campaign.last_action_reason = reason.strip()
            campaign.save(update_fields=["status", "queue_reentry_at", "paused_at", "last_action_reason", "updated_at"])
            event_action = PromotionCampaignAction.ActionType.RESTORED
        else:
            if before in {PromotionCampaign.Status.EXPIRED, PromotionCampaign.Status.REVOKED}:
                raise ValidationError({"campaign": "This campaign is already closed."})
            if before == PromotionCampaign.Status.PAYMENT_REVIEW:
                raise ValidationError({"campaign": "Resolve the payment review before revoking this campaign."})
            if campaign.payment_verified_at and not campaign.verified_payment_id:
                raise ValidationError({"payment": "The verified payment link is missing; investigate the payment before closing the campaign."})
            campaign.status = PromotionCampaign.Status.REVOKED
            campaign.ended_at = now
            campaign.ended_by = actor
            campaign.last_action_reason = reason.strip()
            campaign.remaining_duration_seconds = campaign.remaining_duration_seconds or (
                max(int((campaign.expires_at - now).total_seconds()), 0)
                if campaign.expires_at and before == PromotionCampaign.Status.ACTIVE
                else campaign.duration_days * 86400
            )
            campaign.scheduled_start_at = None
            campaign.save(update_fields=["status", "ended_at", "ended_by", "last_action_reason", "remaining_duration_seconds", "scheduled_start_at", "updated_at"])
            event_action = PromotionCampaignAction.ActionType.REVOKED

        should_refund = (
            action == "REVOKE"
            and campaign.verified_payment_id
            and not confirmed_violation
            and campaign.refund_policy_snapshot == PromotionPlan.RefundPolicy.PRO_RATA
        )
        refund_action = None
        if should_refund:
            remaining_seconds = max(int(campaign.remaining_duration_seconds or 0), 0)
            total_seconds = max(campaign.duration_days * 86400, 1)
            raw_kobo = int(Decimal(campaign.price_kobo) * Decimal(remaining_seconds) / Decimal(total_seconds))
            refund_kobo = (raw_kobo // 100) * 100  # provider accepts whole-NGN refunds in this integration
            if refund_kobo >= 10_000:
                # Refunds for promotion fees use the existing dual-control support refund workflow,
                # but are linked to their own non-escrow payment record.
                from support.services import create_refund_request, create_support_case
                from support.models import SupportCase
                refund_case = create_support_case(
                    opened_by=actor,
                    case_type=SupportCase.CaseType.PAYMENT,
                    subject=f"Promotion refund review — {campaign.plan_name_snapshot}",
                    description=(f"Platform-initiated campaign revocation. Policy: pro-rata unused paid time. "
                                 f"Reason: {reason.strip()} Remaining seconds: {remaining_seconds}; calculated amount: NGN {refund_kobo / 100:.2f}. "
                                 f"Confirmed violation: no. {evidence_summary.strip()}"),
                    promotion_campaign=campaign,
                    priority="HIGH",
                )
                refund_action = create_refund_request(
                    case=refund_case,
                    staff_user=actor,
                    payment=None,
                    promotion_payment=campaign.verified_payment,
                    amount=Decimal(refund_kobo) / Decimal(100),
                    reason=f"Pro-rata refund under accepted campaign terms: {reason.strip()}",
                    idempotency_key=f"promotion-refund:{campaign.id}:{campaign.verified_payment_id}",
                )
        audit_event = _campaign_action(
            campaign,
            action=event_action,
            actor=actor,
            reason=reason.strip(),
            evidence_summary=evidence_summary.strip(),
            support_case_id=case.pk,
            status_before=before,
            status_after=campaign.status,
            refund_action_id=refund_action.pk if refund_action else None,
            metadata={"confirmed_violation": bool(confirmed_violation), "refund_policy": campaign.refund_policy_snapshot},
        )
        from support.models import SupportCaseEvent
        SupportCaseEvent.objects.create(
            case=case,
            action=f"PROMOTION_CAMPAIGN_{event_action}",
            performed_by=actor,
            note=reason.strip(),
            metadata={"campaign_id": str(campaign.id), "evidence_summary": evidence_summary.strip(), "confirmed_violation": bool(confirmed_violation), "refund_action_id": str(refund_action.id) if refund_action else None},
        )
        create_audit_log(action=f"PROMOTION_CAMPAIGN_{event_action}", resource_type="PromotionCampaign", resource_id=str(campaign.id), actor=actor, metadata={"status_before": before, "status_after": campaign.status, "support_case_id": case.pk, "reason": reason.strip(), "confirmed_violation": bool(confirmed_violation), "refund_action_id": str(refund_action.id) if refund_action else None})
        if action in {"PAUSE", "SUSPEND", "RESTORE", "REVOKE"}:
            actor_name = (actor.get_full_name() or getattr(actor, "email", ""))[:160] if actor else "Authorized support staff"
            schedule_correction_reason = f"Recomputed paid schedule after {event_action} by {actor_name}: {reason.strip()}"
            transaction.on_commit(
                lambda inventory_id=str(inventory.id), schedule_actor=actor, correction_reason=schedule_correction_reason: recompute_inventory_schedule(
                    inventory_id,
                    actor=schedule_actor,
                    correction_reason=correction_reason,
                )
            )
            transaction.on_commit(invalidate_public_discovery_cache)
        notification_reason = reason.strip()
        transaction.on_commit(
            lambda campaign=campaign, action=event_action, note=notification_reason: _notify_campaign(campaign, action, note)
        )
    return campaign, audit_event, refund_action


def record_campaign_impressions(campaign_ids):
    ids = [str(value) for value in campaign_ids if value]
    if not ids:
        return
    PromotionCampaign.objects.filter(pk__in=ids, status=PromotionCampaign.Status.ACTIVE).update(impressions=F("impressions") + 1)
