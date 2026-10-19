"""Transactional application lifecycle, immutable review snapshots and notifications."""

import logging
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from home.models import ArtisanService, Institution, Locations
from services.image_utils import optimize_profile_photo

from notifications.events import (
    notify_promotion_application_decision,
    notify_promotion_application_submitted,
)

from .eligibility import promotion_eligibility
from .models import (
    PromotionApplication,
    PromotionApplicationDecision,
    PromotionApplicationEvidence,
    PromotionApplicationRevision,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = (
    PromotionApplication.Status.PENDING_REVIEW,
    PromotionApplication.Status.CHANGES_REQUIRED,
    PromotionApplication.Status.APPROVED,
)


def _location_snapshot(location):
    if location is None:
        return None
    return {
        "id": str(location.pk),
        "country": location.country,
        "state": location.state,
        "area": location.area,
    }


def _target_snapshot(application):
    if application.promotion_type == PromotionApplication.PromotionType.INSTITUTION:
        institution = application.institution
        return {
            "kind": "INSTITUTION",
            "id": str(institution.pk),
            "name": institution.name,
            "campus": institution.campus,
            "abbreviation": institution.abbreviation,
            "location": _location_snapshot(institution.location),
        }
    if application.promotion_type == PromotionApplication.PromotionType.AREA:
        location = application.area_location
        return {"kind": "AREA", **_location_snapshot(location)}
    if application.promotion_type == PromotionApplication.PromotionType.STATE:
        return {
            "kind": "STATE",
            "country": application.state_country,
            "state": application.state_name,
        }
    return {"kind": "HOMEPAGE", "label": "Homepage Feature"}


def _service_snapshot(service_offerings):
    snapshot = []
    for offering in service_offerings:
        location = offering.location
        snapshot.append({
            "artisan_service_id": offering.pk,
            "service_id": offering.service_id,
            "service_name": offering.service.name,
            "category_name": offering.service.category.name,
            "location": _location_snapshot(location),
            "starting_price": str(offering.starting_price) if offering.starting_price is not None else None,
        })
    return snapshot


def _profile_snapshot(artisan):
    verification = getattr(artisan, "verification", None)
    photo_name = artisan.profile_photo.name if artisan.profile_photo else ""
    try:
        photo_url = artisan.profile_photo.url if artisan.profile_photo else ""
    except (ValueError, OSError):
        photo_url = ""
    links = []
    for index in (1, 2):
        url = getattr(artisan, f"social_media_{index}_url", "")
        platform = getattr(artisan, f"social_media_{index}_platform", "")
        if url and platform:
            links.append({
                "platform": platform,
                "platform_label": getattr(artisan, f"get_social_media_{index}_platform_display")(),
                "url": url,
            })
    return {
        "id": str(artisan.pk),
        "name": artisan.user.get_full_name(),
        "business_name": artisan.business_name,
        "bio": artisan.bio,
        "years_experience": artisan.years_experience,
        "profile_status": artisan.profile_status,
        "verification_status": verification.verification_status if verification else "UNVERIFIED",
        "registered_location": {
            "country": artisan.country,
            "state": artisan.state,
            "area": artisan.area,
        },
        "profile_photo_name": photo_name,
        "profile_photo_url": photo_url,
        "average_rating": str(artisan.average_rating),
        "review_count": artisan.reviews_received.count(),
        "social_links": links,
        "portfolio_url": artisan.portfolio_url,
    }


def _snapshot_for(application, revision, service_offerings):
    evidence = list(revision.evidence.all())
    return {
        "revision_number": revision.revision_number,
        "submitted_at": revision.submitted_at.isoformat(),
        "promotion_type": application.promotion_type,
        "promotion_type_label": application.get_promotion_type_display(),
        "target": _target_snapshot(application),
        "promotional_description": application.promotional_description,
        "services": _service_snapshot(service_offerings),
        "profile": _profile_snapshot(application.artisan),
        "work_samples": [
            {
                "evidence_id": str(item.pk),
                "caption": item.caption,
                "file_name": item.image.name.rsplit("/", 1)[-1],
                "uploaded_at": item.uploaded_at.isoformat(),
            }
            for item in evidence
        ],
        "eligibility_at_submission": promotion_eligibility(application.artisan),
    }


def _store_evidence(revision, uploads, staged_files):
    evidence_items = []
    for upload in uploads:
        optimized = optimize_profile_photo(upload)
        filename = f"{uuid4().hex}.webp"
        evidence = PromotionApplicationEvidence(revision=revision)
        evidence.image.save(filename, optimized, save=False)
        evidence.save()
        staged_files.append((evidence.image.storage, evidence.image.name))
        evidence_items.append(evidence)
    return evidence_items


def _target_key(promotion_type, *, institution=None, area_location=None, state_country="", state_name=""):
    if promotion_type == PromotionApplication.PromotionType.INSTITUTION:
        return {"institution": institution}
    if promotion_type == PromotionApplication.PromotionType.AREA:
        return {"area_location": area_location}
    if promotion_type == PromotionApplication.PromotionType.STATE:
        return {"state_country": state_country, "state_name": state_name}
    return {}


def _check_no_duplicate_open_application(artisan, promotion_type, target, *, exclude=None):
    queryset = PromotionApplication.objects.filter(
        artisan=artisan,
        promotion_type=promotion_type,
        status__in=OPEN_STATUSES,
        **target,
    )
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
    if queryset.exists():
        raise ValidationError({
            "promotion_type": "An application for this exact promotion type and target is already awaiting review, changes or purchase.",
        })


def _create_revision(application, service_offerings, uploads, staged_files):
    number = application.revisions.count() + 1
    revision = PromotionApplicationRevision.objects.create(
        application=application,
        revision_number=number,
        snapshot={},
    )
    _store_evidence(revision, uploads, staged_files)
    revision.snapshot = _snapshot_for(application, revision, service_offerings)
    revision.save(update_fields=["snapshot"])
    return revision


def _notify_submission(application, revision):
    notify_promotion_application_submitted(application, revision)


def create_promotion_application(*, artisan, validated_data):
    """Create a submitted application and its first immutable review snapshot."""
    staged_files = []
    try:
        with transaction.atomic():
            target = _target_key(
                validated_data["promotion_type"],
                institution=validated_data.get("institution"),
                area_location=validated_data.get("area_location"),
                state_country=validated_data.get("state_country", ""),
                state_name=validated_data.get("state_name", ""),
            )
            _check_no_duplicate_open_application(artisan, validated_data["promotion_type"], target)
            now = timezone.now()
            application = PromotionApplication.objects.create(
                artisan=artisan,
                promotion_type=validated_data["promotion_type"],
                institution=validated_data.get("institution"),
                area_location=validated_data.get("area_location"),
                state_country=validated_data.get("state_country", ""),
                state_name=validated_data.get("state_name", ""),
                promotional_description=validated_data["promotional_description"],
                status=PromotionApplication.Status.PENDING_REVIEW,
                submitted_at=now,
            )
            service_offerings = list(validated_data["service_offerings"])
            application.services.set(service_offerings)
            revision = _create_revision(
                application,
                service_offerings,
                validated_data.get("evidence_images", []),
                staged_files,
            )
            _notify_submission(application, revision)
            return application
    except Exception:
        for storage, name in staged_files:
            try:
                storage.delete(name)
            except Exception:
                logger.warning("Could not remove staged promotion evidence after rollback.", exc_info=True)
        raise


def resubmit_promotion_application(*, application, artisan, validated_data):
    """Apply requested corrections as a new immutable revision, then return to review."""
    staged_files = []
    try:
        with transaction.atomic():
            locked = PromotionApplication.objects.select_for_update().get(pk=application.pk)
            if locked.artisan_id != artisan.pk:
                raise ValidationError({"detail": "This application does not belong to the signed-in artisan."})
            if locked.status != PromotionApplication.Status.CHANGES_REQUIRED:
                raise ValidationError({"status": "Only an application marked Changes Required can be edited and resubmitted."})

            target = _target_key(
                validated_data["promotion_type"],
                institution=validated_data.get("institution"),
                area_location=validated_data.get("area_location"),
                state_country=validated_data.get("state_country", ""),
                state_name=validated_data.get("state_name", ""),
            )
            _check_no_duplicate_open_application(
                artisan,
                validated_data["promotion_type"],
                target,
                exclude=locked,
            )

            locked.promotion_type = validated_data["promotion_type"]
            locked.institution = validated_data.get("institution")
            locked.area_location = validated_data.get("area_location")
            locked.state_country = validated_data.get("state_country", "")
            locked.state_name = validated_data.get("state_name", "")
            locked.promotional_description = validated_data["promotional_description"]
            locked.status = PromotionApplication.Status.PENDING_REVIEW
            locked.submitted_at = timezone.now()
            locked.save(update_fields=[
                "promotion_type", "institution", "area_location", "state_country", "state_name",
                "promotional_description", "status", "submitted_at", "updated_at",
            ])

            service_offerings = list(validated_data["service_offerings"])
            locked.services.set(service_offerings)
            revision = _create_revision(
                locked,
                service_offerings,
                validated_data.get("evidence_images", []),
                staged_files,
            )
            _notify_submission(locked, revision)
            return locked
    except Exception:
        for storage, name in staged_files:
            try:
                storage.delete(name)
            except Exception:
                logger.warning("Could not remove staged promotion evidence after rollback.", exc_info=True)
        raise


def withdraw_promotion_application(*, application, artisan):
    """Withdraw an owned application before it proceeds to checkout, preserving the audit history."""
    with transaction.atomic():
        locked = PromotionApplication.objects.select_for_update().get(pk=application.pk)
        if locked.artisan_id != artisan.pk:
            raise ValidationError({"detail": "This application does not belong to the signed-in artisan."})
        if locked.status not in {
            PromotionApplication.Status.PENDING_REVIEW,
            PromotionApplication.Status.CHANGES_REQUIRED,
            PromotionApplication.Status.APPROVED,
        }:
            raise ValidationError({"status": "Only Pending Review, Changes Required, or Approved applications can be withdrawn."})
        revision = locked.revisions.order_by("-revision_number").first()
        now = timezone.now()
        locked.status = PromotionApplication.Status.WITHDRAWN
        locked.reviewed_at = now
        locked.save(update_fields=["status", "reviewed_at", "updated_at"])
        if revision:
            PromotionApplicationDecision.objects.create(
                application=locked,
                revision=revision,
                decision_type=PromotionApplicationDecision.DecisionType.WITHDRAWN,
                reason="Withdrawn by the applicant.",
                actor=artisan.user,
                actor_name_snapshot=artisan.user.get_full_name(),
            )
        return locked


def _approval_eligibility_errors(application, revision):
    """Recheck that live eligibility still holds for the exact revision Support reviewed."""
    artisan = application.artisan
    eligibility = promotion_eligibility(artisan)
    errors = [item["message"] for item in eligibility["requirements"]]
    snapshot = revision.snapshot if isinstance(revision.snapshot, dict) else {}

    if snapshot.get("promotion_type") != application.promotion_type:
        errors.append("The current application product no longer matches the submitted revision being reviewed.")
    submitted_target = snapshot.get("target")
    try:
        current_target = _target_snapshot(application)
    except (AttributeError, TypeError):
        current_target = None
    if not isinstance(submitted_target, dict) or current_target != submitted_target:
        errors.append("The target details changed after submission; request a new revision before approving.")

    if application.promotion_type == PromotionApplication.PromotionType.INSTITUTION:
        if not application.institution_id or not application.institution.is_active:
            errors.append("The selected institution is no longer active in the supported catalogue.")
        target_location = application.institution.location if application.institution_id else None
    elif application.promotion_type == PromotionApplication.PromotionType.AREA:
        target_location = application.area_location
        if target_location is None:
            errors.append("The selected area is no longer available in the supported location catalogue.")
    elif application.promotion_type == PromotionApplication.PromotionType.STATE:
        target_location = None
        if not Locations.objects.filter(country=application.state_country, state=application.state_name).exists():
            errors.append("The selected state is no longer supported by the marketplace location catalogue.")
    else:
        target_location = None

    if application.promotional_description != snapshot.get("promotional_description"):
        errors.append("The promotion description changed after submission; request a new revision before approving.")

    submitted_services = snapshot.get("services")
    if not isinstance(submitted_services, list) or not submitted_services:
        errors.append("This submitted revision does not contain a valid selected-service snapshot.")
        submitted_services = []
    reviewed_ids = []
    for item in submitted_services:
        try:
            reviewed_ids.append(int(item["artisan_service_id"]))
        except (KeyError, TypeError, ValueError):
            errors.append("The submitted service snapshot is incomplete; request a new revision before approving.")
    if len(reviewed_ids) != len(set(reviewed_ids)):
        errors.append("The submitted service snapshot contains duplicate offerings.")

    current_relation_ids = set(application.services.values_list("pk", flat=True))
    if set(reviewed_ids) != current_relation_ids:
        errors.append("The selected services changed after submission; request a new revision before approving.")

    offerings = list(
        ArtisanService.objects.filter(pk__in=reviewed_ids, artisan_id=application.artisan_id)
        .select_related("service__category", "location")
        .order_by("service__name", "pk")
    )
    offering_by_id = {offering.pk: offering for offering in offerings}
    if set(offering_by_id) != set(reviewed_ids):
        errors.append("One or more selected service offerings are no longer available on the artisan profile.")

    active_offerings = []
    for submitted in submitted_services:
        try:
            offering_id = int(submitted["artisan_service_id"])
        except (KeyError, TypeError, ValueError):
            continue
        offering = offering_by_id.get(offering_id)
        if offering is None:
            continue
        if not offering.service.is_active:
            errors.append(f"{offering.service.name} is no longer an active marketplace service.")
            continue
        current_service_snapshot = _service_snapshot([offering])[0]
        if current_service_snapshot != submitted:
            errors.append(f"{offering.service.name} details changed after submission; request a new revision before approving.")
        active_offerings.append(offering)

    if not active_offerings:
        errors.append("At least one selected active service offering is required to approve this application.")
    for offering in active_offerings:
        if offering.location_id is None:
            continue
        if application.promotion_type in {
            PromotionApplication.PromotionType.INSTITUTION,
            PromotionApplication.PromotionType.AREA,
        } and (target_location is None or offering.location_id != target_location.pk):
            errors.append(f"{offering.service.name} is no longer offered at the exact requested locality or institution location.")
        elif application.promotion_type == PromotionApplication.PromotionType.STATE and (
            offering.location.country != application.state_country
            or offering.location.state != application.state_name
        ):
            errors.append(f"{offering.service.name} is no longer offered in the exact requested state.")
    return list(dict.fromkeys(errors))


def decide_promotion_application(*, application, reviewer, decision_type, reason):
    """Record an authorized support decision against the exact latest submitted revision."""
    with transaction.atomic():
        locked = (
            PromotionApplication.objects.select_for_update()
            .select_related("artisan__user", "institution__location", "area_location")
            .get(pk=application.pk)
        )
        if locked.status != PromotionApplication.Status.PENDING_REVIEW:
            raise ValidationError({"status": "Only applications currently Pending Review can receive a decision."})
        revision = locked.revisions.order_by("-revision_number").first()
        if revision is None:
            raise ValidationError({"status": "This application has no submitted revision to review."})
        if decision_type == PromotionApplicationDecision.DecisionType.APPROVED:
            eligibility_errors = _approval_eligibility_errors(locked, revision)
            if eligibility_errors:
                raise ValidationError({"eligibility": eligibility_errors})

        status_by_decision = {
            PromotionApplicationDecision.DecisionType.APPROVED: PromotionApplication.Status.APPROVED,
            PromotionApplicationDecision.DecisionType.CHANGES_REQUIRED: PromotionApplication.Status.CHANGES_REQUIRED,
            PromotionApplicationDecision.DecisionType.REJECTED: PromotionApplication.Status.REJECTED,
        }
        new_status = status_by_decision.get(decision_type)
        if new_status is None:
            raise ValidationError({"decision": "Select Approve, Request Changes, or Reject."})
        now = timezone.now()
        locked.status = new_status
        locked.reviewed_at = now
        locked.last_reviewed_by = reviewer
        locked.save(update_fields=["status", "reviewed_at", "last_reviewed_by", "updated_at"])
        decision = PromotionApplicationDecision.objects.create(
            application=locked,
            revision=revision,
            decision_type=decision_type,
            reason=reason,
            actor=reviewer,
            actor_name_snapshot=(reviewer.get_full_name() or reviewer.email)[:160],
        )
        notify_promotion_application_decision(locked, decision)
        return locked, decision
