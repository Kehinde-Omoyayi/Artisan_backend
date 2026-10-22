import logging
from datetime import date, time as datetime_time
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from booking.models import Booking, BookingStatusHistory
from home.models import (
    ArtisanProfile,
    ArtisanService,
    CustomerProfile,
    Service,
    ServiceCategory,
    Locations,
)
from payments.services import prepare_booking_payment
from support.services import create_support_case
from support.models import SupportCase
from services.models import ServiceRequest

from .events import money_text, record_booking_event, record_conversation_event
from .models import (
    Conversation,
    ConversationAttachment,
    ConversationReadState,
    Message,
    Offer,
)
from .security import MAX_ATTACHMENTS_PER_MESSAGE, validate_attachment
from .state import derive_conversation_state
from core.audit import audit_request_action
from core.state import BOOKING_ACTIVE_STATUSES

logger = logging.getLogger(__name__)


REQUEST_FIELDS = {
    "service_id",
    "category_id",
    "description",
    "location_id",
    "preferred_date",
    "preferred_time_start",
    "preferred_time_end",
    "preferred_time_note",
    "urgency",
    "budget",
    "budget_unknown",
    "additional_details",
    "country",
    "state",
    "area",
}


def get_conversation_for_user(*, user, conversation_id, lock=False):
    queryset = Conversation.objects.select_related(
        "customer",
        "artisan__user",
        "service_request__category",
        "service_request__location",
        "service_request__service",
    ).prefetch_related("participants__artisan_profile")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    conversation = (
        queryset.filter(id=conversation_id)
        .filter(Q(participants=user) | Q(customer=user) | Q(artisan__user=user))
        .distinct()
        .first()
    )
    if not conversation:
        raise NotFound("Conversation not found.")
    return conversation


def get_artisan_or_403(user, artisan_id, allow_self=False):
    def _load():
        try:
            artisan = (
                ArtisanProfile.objects.select_related("user", "verification")
                .select_for_update(of=("self",))
                .get(pk=artisan_id)
            )
        except ArtisanProfile.DoesNotExist as exc:
            raise NotFound("Artisan not found.") from exc
        if not allow_self and artisan.user_id == user.id:
            raise PermissionDenied(
                "You cannot start a service conversation with yourself."
            )
        if (
            not artisan.user.is_active
            or artisan.profile_status != "ACTIVE"
            or getattr(artisan, "verification", None) is None
            or artisan.verification.verification_status != "VERIFIED"
        ):
            raise PermissionDenied(
                "The artisan is not currently available for a service conversation."
            )
        return artisan

    if transaction.get_autocommit():
        with transaction.atomic():
            return _load()
    return _load()


def validate_service_for_artisan(*, artisan, service_id):
    try:
        service = Service.objects.select_related("category").get(
            pk=service_id, is_active=True
        )
    except Service.DoesNotExist as exc:
        raise ValidationError(
            {"service_id": "Selected service is not available."}
        ) from exc
    if (
        artisan is not None
        and not ArtisanService.objects.filter(artisan=artisan, service=service).exists()
    ):
        raise ValidationError(
            {"service_id": "The selected artisan does not provide this service."}
        )
    return service


def default_service_for_artisan(artisan):
    if artisan is None:
        return None
    service_ids = list(
        artisan.artisan_services.filter(service__is_active=True)
        .order_by("service_id")
        .values_list("service_id", flat=True)
        .distinct()[:2]
    )
    if len(service_ids) != 1:
        return None
    return (
        Service.objects.select_related("category")
        .filter(pk=service_ids[0], is_active=True)
        .first()
    )


def validate_artisan_location(*, artisan, service, location_id):
    if artisan is None:
        try:
            return Locations.objects.get(pk=location_id)
        except (Locations.DoesNotExist, ValueError):
            raise ValidationError({"location_id": "Choose a real service locality."})
    try:
        location = Locations.objects.get(pk=location_id)
    except Locations.DoesNotExist as exc:
        raise ValidationError(
            {"location_id": "Selected location was not found."}
        ) from exc
    if (
        not ArtisanService.objects.filter(artisan=artisan, service=service)
        .filter(Q(location=location) | Q(location__isnull=True))
        .exists()
    ):
        raise ValidationError(
            {
                "location_id": "The artisan does not offer this service in the selected location."
            }
        )
    return location


def _normalize_date(value):
    if value in (None, ""):
        return None
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError(
            {"preferred_date": "Use YYYY-MM-DD for preferred_date."}
        ) from exc
    if parsed < timezone.localdate():
        raise ValidationError(
            {"preferred_date": "Preferred date cannot be in the past."}
        )
    return parsed


def _normalize_money(value):
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({"budget": "Budget must be a valid amount."}) from exc
    if not amount.is_finite():
        raise ValidationError({"budget": "Budget must be a finite amount."})
    if amount < 0:
        raise ValidationError({"budget": "Budget cannot be negative."})
    if int(amount * 100) > getattr(
        settings, "MAX_SERVICE_AMOUNT_KOBO", 100_000_000_000
    ):
        raise ValidationError(
            {"budget": "Budget exceeds the platform transaction limit."}
        )
    return amount


def _normalize_time(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, datetime_time):
        parsed = value
    else:
        try:
            parsed = datetime_time.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Use a valid 24-hour time."}) from exc
    if parsed.tzinfo is not None:
        raise ValidationError({field: "A preferred time must not include a time zone."})
    return parsed.isoformat(timespec="minutes")


def _time_for_model(value):
    if value in (None, ""):
        return None
    return datetime_time.fromisoformat(str(value))


def _request_ready(draft):
    return (
        bool(draft.get("description"))
        and bool(draft.get("service_id"))
        and bool(draft.get("location_id"))
        and bool(draft.get("preferred_date"))
    )


def _draft_from_request(service_request):
    location = service_request.location
    return {
        "description": service_request.description,
        "additional_details": service_request.additional_details or "",
        "service_id": (
            str(service_request.service_id) if service_request.service_id else None
        ),
        "location_id": str(service_request.location_id),
        "country": location.country,
        "state": location.state,
        "area": location.area,
        "preferred_date": (
            service_request.preferred_date.isoformat()
            if service_request.preferred_date
            else None
        ),
        "preferred_time_start": (
            service_request.preferred_time_start.isoformat(timespec="minutes")
            if service_request.preferred_time_start
            else None
        ),
        "preferred_time_end": (
            service_request.preferred_time_end.isoformat(timespec="minutes")
            if service_request.preferred_time_end
            else None
        ),
        "preferred_time_note": service_request.preferred_time_note or "",
        "category_id": str(service_request.category_id),
        "urgency": service_request.urgency,
        "budget": (
            str(service_request.budget) if service_request.budget is not None else None
        ),
        "budget_unknown": service_request.budget is None,
    }


def _request_missing_errors(draft):
    errors = {}
    if not draft.get("service_id"):
        errors["service_id"] = "Select a service before submitting the request."
    if not str(draft.get("description") or "").strip():
        errors["description"] = "Describe the work before submitting the request."
    if not draft.get("location_id"):
        errors["location_id"] = (
            "Select a catalogue location before submitting the request."
        )
    if not draft.get("preferred_date"):
        errors["preferred_date"] = (
            "Choose a preferred date before submitting the request."
        )
    return errors


def apply_request_patch(*, conversation, user, payload):
    """Save an explicit draft; only submit_request=True submits a real request."""
    if conversation.conversation_type != Conversation.ConversationType.SERVICE:
        raise ValidationError(
            {"request": "Direct conversations do not have a service request."}
        )
    if conversation.customer_id != user.id:
        raise PermissionDenied("Only the customer can update the service request.")

    payload = dict(payload or {})
    submit_request = payload.pop("submit_request", False)
    close_request = payload.pop("close_request", False)
    if submit_request not in (None, True, False):
        raise ValidationError({"submit_request": "Expected a boolean value."})
    unknown = set(payload) - REQUEST_FIELDS
    if unknown:
        raise ValidationError(
            {
                field: "This field is not accepted by the conversation request API."
                for field in sorted(unknown)
            }
        )

    with transaction.atomic():
        if conversation.service_request_id:
            ServiceRequest.objects.select_for_update().get(
                pk=conversation.service_request_id
            )
        conversation = (
            Conversation.objects.select_for_update(of=("self",))
            .select_related(
                "customer",
                "artisan__user",
                "service_request__location",
                "service_request__service",
                "service_request__category",
            )
            .get(pk=conversation.pk)
        )
        if (
            conversation.service_request_id
            and Booking.objects.filter(
                request_id=conversation.service_request_id
            ).exists()
        ):
            raise ValidationError(
                {
                    "request": "The service request cannot be changed after a booking exists."
                }
            )

        if close_request:
            if submit_request:
                raise ValidationError("Close and submit are separate actions.")
            req = conversation.service_request
            if Booking.objects.filter(request=req).exists():
                raise ValidationError(
                    "Use the booking cancellation/completion workflow."
                )
            if req.status not in ("DRAFT", "OPEN", "ARCHIVED", "CLOSED"):
                raise ValidationError("This request cannot be closed here.")
            if req.status != "CLOSED":
                was_open = req.status == "OPEN"
                req.status = "CLOSED"
                req.closed_at = timezone.now()
                req.save(update_fields=["status", "closed_at", "updated_at"])
                conversation.direct_key = None
                conversation.save(update_fields=["direct_key", "updated_at"])
                if was_open:
                    from notifications.events import notify_service_request_withdrawn

                    notify_service_request_withdrawn(req)
                record_conversation_event(
                    conversation=conversation,
                    event_code="REQUEST_CLOSED",
                    event_key=f"request:{req.pk}:closed",
                    body="Unbooked request closed. Recordings are retained for seven days unless a preservation hold applies.",
                )
            return None, conversation
        if conversation.service_request.status == "CLOSED":
            raise ValidationError(
                "This request is closed. Start a new service request."
            )
        draft = dict(conversation.draft or {})
        if conversation.request_submitted and not draft:
            # Partial edits merge into the real request, without discarding saved values.
            draft = _draft_from_request(conversation.service_request)

        budget_was_supplied = "budget" in payload
        budget_unknown_was_supplied = "budget_unknown" in payload
        normalized_budget = None
        normalized_budget_unknown = None

        for key, value in payload.items():
            if key == "description":
                value = str(value or "").strip()
                if (not value and submit_request is not False) or len(value) > 10000:
                    raise ValidationError(
                        {
                            "description": "Description is required and must be 10,000 characters or fewer."
                        }
                    )
            elif key == "additional_details":
                value = str(value or "").strip()
                if len(value) > 5000:
                    raise ValidationError(
                        {
                            "additional_details": "Additional details must be 5,000 characters or fewer."
                        }
                    )
            elif key == "preferred_date":
                parsed = _normalize_date(value)
                value = parsed.isoformat() if parsed else None
            elif key in {"preferred_time_start", "preferred_time_end"}:
                value = _normalize_time(value, key)
            elif key == "preferred_time_note":
                value = str(value or "").strip()
                if len(value) > 100:
                    raise ValidationError({key: "Use 100 characters or fewer."})
            elif key == "budget":
                normalized_budget = (
                    str(_normalize_money(value)) if value not in (None, "") else None
                )
                value = normalized_budget
            elif key == "budget_unknown":
                normalized_budget_unknown = bool(value)
                value = normalized_budget_unknown
            elif key in {"country", "state", "area"}:
                value = str(value or "").strip()
                max_length = {"country": 100, "state": 100, "area": 150}[key]
                if len(value) > max_length:
                    raise ValidationError(
                        {key: f"Use {max_length} characters or fewer."}
                    )
            elif key == "service_id":
                value = str(int(value)) if value not in (None, "") else None
            elif key == "category_id":
                value = str(value).strip() if value not in (None, "") else None
            elif key == "location_id":
                value = str(value).strip() if value not in (None, "") else None
            draft[key] = value

        if budget_unknown_was_supplied:
            draft["budget_unknown"] = normalized_budget_unknown
            if normalized_budget_unknown:
                draft["budget"] = None
        elif budget_was_supplied and normalized_budget is not None:
            draft["budget_unknown"] = False

        artisan = conversation.artisan
        service = None
        category_id = draft.get("category_id")
        if "service_id" in payload:
            if draft.get("service_id"):
                service = validate_service_for_artisan(
                    artisan=artisan, service_id=draft["service_id"]
                )
                if category_id and str(service.category_id) != str(category_id):
                    raise ValidationError(
                        {
                            "category_id": "The selected service does not belong to this category."
                        }
                    )
            else:
                draft.pop("service_id", None)
        elif "category_id" in payload:
            if draft.get("service_id"):
                existing = validate_service_for_artisan(
                    artisan=artisan, service_id=draft["service_id"]
                )
                if str(existing.category_id) == str(category_id):
                    service = existing
                else:
                    draft.pop("service_id", None)
            if service is None and category_id and artisan is not None:
                offered = list(
                    ArtisanService.objects.filter(
                        artisan=artisan,
                        service__category_id=category_id,
                        service__is_active=True,
                    )
                    .select_related("service__category")
                    .order_by("service_id")[:2]
                )
                if not offered:
                    raise ValidationError(
                        {
                            "category_id": "The selected artisan does not provide a service in this category."
                        }
                    )
                if len(offered) == 1:
                    service = offered[0].service
                    draft["service_id"] = str(service.pk)
                else:
                    # Keep the category selection, but never pick an arbitrary service.
                    draft.pop("service_id", None)
        elif draft.get("service_id"):
            service = validate_service_for_artisan(
                artisan=artisan, service_id=draft["service_id"]
            )
        else:
            service = default_service_for_artisan(artisan)
            if service:
                draft["service_id"] = str(service.pk)
                draft["category_id"] = str(service.category_id)

        if category_id and not ServiceCategory.objects.filter(pk=category_id).exists():
            raise ValidationError({"category_id": "Unknown category."})
        if service:
            draft["category_id"] = str(service.category_id)
        elif category_id:
            draft["category_id"] = str(category_id)

        location = None
        if draft.get("location_id"):
            try:
                location = Locations.objects.get(pk=draft["location_id"])
            except (Locations.DoesNotExist, ValueError, TypeError) as exc:
                raise ValidationError(
                    {"location_id": "Selected location was not found in the catalogue."}
                ) from exc
            if service:
                location = validate_artisan_location(
                    artisan=artisan, service=service, location_id=location.pk
                )
            for key, actual in (
                ("country", location.country),
                ("state", location.state),
                ("area", location.area),
            ):
                supplied = str(payload.get(key) or "").strip() if key in payload else ""
                if supplied and supplied.casefold() != actual.casefold():
                    raise ValidationError(
                        {
                            key: "This value does not match the selected catalogue location."
                        }
                    )
                draft[key] = actual

        start_time = draft.get("preferred_time_start")
        end_time = draft.get("preferred_time_end")
        if start_time and end_time:
            if datetime_time.fromisoformat(end_time) <= datetime_time.fromisoformat(
                start_time
            ):
                raise ValidationError(
                    {"preferred_time_end": "End time must be later than start time."}
                )

        conversation.draft = draft
        service_request = None
        complete = _request_ready(draft)
        should_submit = submit_request is True
        if submit_request is True and not complete:
            raise ValidationError(_request_missing_errors(draft))
        if should_submit:
            if service is None:
                raise ValidationError(
                    {"service_id": "Select a service before completing the request."}
                )
            if location is None:
                location = validate_artisan_location(
                    artisan=artisan, service=service, location_id=draft["location_id"]
                )
            service_request = conversation.service_request
            request_values = {
                "service": service,
                "category": service.category,
                "location": location,
                "preferred_date": _normalize_date(draft["preferred_date"]),
                "preferred_time_start": _time_for_model(start_time),
                "preferred_time_end": _time_for_model(end_time),
                "preferred_time_note": draft.get("preferred_time_note") or "",
                "urgency": draft.get("urgency") or "NORMAL",
                "budget": (
                    None
                    if draft.get("budget_unknown")
                    else _normalize_money(draft.get("budget"))
                ),
                "description": str(draft.get("description") or "").strip(),
                "additional_details": draft.get("additional_details") or "",
            }
            if service_request is None or service_request.status == "DRAFT":
                try:
                    if service_request is None:
                        service_request = ServiceRequest.objects.create(
                            requester=user, **request_values
                        )
                    else:
                        for field, value in request_values.items():
                            setattr(service_request, field, value)
                        service_request.status = "OPEN"
                        service_request.save(
                            update_fields=[*request_values, "status", "updated_at"]
                        )
                except IntegrityError as exc:
                    raise ValidationError(
                        {
                            "request": "Unable to create the service request due to a concurrent update."
                        }
                    ) from exc
                conversation.service_request = service_request
                conversation.draft = {}
                conversation.direct_key = None
                conversation.save(
                    update_fields=[
                        "service_request",
                        "draft",
                        "direct_key",
                        "updated_at",
                    ]
                )
                record_conversation_event(
                    conversation=conversation,
                    event_code="REQUEST_SUBMITTED",
                    event_key=f"request:{service_request.id}:submitted",
                    metadata={"service_request_id": service_request.id},
                    body="Request submitted. An artisan can discuss the work and prepare a formal quote. No booking or payment has been confirmed.",
                )
                try:
                    from notifications.events import notify_service_request_created

                    notify_service_request_created(service_request)
                except ImportError:
                    pass
            else:
                changed = any(
                    getattr(service_request, field) != value
                    for field, value in request_values.items()
                )
                for field, value in request_values.items():
                    setattr(service_request, field, value)
                service_request.save(
                    update_fields=[*request_values.keys(), "updated_at"]
                )
                if changed:
                    from notifications.events import notify_service_request_updated

                    notify_service_request_updated(service_request)
                conversation.draft = {}
                conversation.save(update_fields=["draft", "updated_at"])
        else:
            conversation.save(update_fields=["draft", "updated_at"])

        from core.services import create_audit_log

        create_audit_log(
            action="SERVICE_REQUEST_DRAFT_UPDATED",
            resource_type="Conversation",
            resource_id=str(conversation.id),
            actor=user,
            metadata={
                "fields": sorted(payload.keys()),
                "submitted": bool(service_request),
            },
        )

    return service_request, conversation


def start_conversation(*, user, artisan_id, service_id=None):
    if not hasattr(user, "customer_profile"):
        raise PermissionDenied("Only a customer can start a service conversation.")
    with transaction.atomic():
        from django.contrib.auth import get_user_model

        get_user_model().objects.select_for_update().get(pk=user.pk)
        artisan = get_artisan_or_403(user, artisan_id)
        latest_booking = Booking.objects.filter(
            request_id=OuterRef("service_request_id")
        ).order_by("-created_at")
        candidates = (
            Conversation.objects.select_for_update(of=("self",))
            .select_related("artisan__user", "customer")
            .filter(customer=user, artisan=artisan)
            .annotate(
                latest_booking_status=Subquery(latest_booking.values("status")[:1])
            )
            .order_by("-updated_at")
        )
        conversation = None
        terminal_statuses = {
            Booking.Status.COMPLETED,
            Booking.Status.CANCELLED,
            Booking.Status.REJECTED,
        }
        for candidate in candidates:
            if candidate.service_request_id and candidate.service_request.status in (
                "CLOSED",
                "COMPLETED",
                "ARCHIVED",
            ):
                continue
            if (
                candidate.service_request_id is None
                or candidate.latest_booking_status not in terminal_statuses
            ):
                conversation = candidate
                break
        created = False
        if conversation is None:
            try:
                with transaction.atomic():
                    conversation = Conversation.objects.create(
                        customer=user,
                        artisan=artisan,
                        conversation_type=Conversation.ConversationType.SERVICE,
                    )
                    conversation.participants.set([user, artisan.user])
                created = True
            except IntegrityError:
                conversation = (
                    Conversation.objects.select_for_update(of=("self",))
                    .filter(
                        customer=user, artisan=artisan, service_request__isnull=True
                    )
                    .order_by("-updated_at")
                    .first()
                )
                if conversation is None:
                    raise ValidationError(
                        "Unable to start a service conversation due to a concurrent update."
                    ) from None
        conversation.participants.set([user, artisan.user])
        if not conversation.request_submitted:
            draft = dict(conversation.draft or {})
            service = (
                validate_service_for_artisan(artisan=artisan, service_id=service_id)
                if service_id is not None
                else default_service_for_artisan(artisan)
            )
            if service is not None:
                draft.update(
                    {"service_id": str(service.id), "category_id": service.category_id}
                )
                conversation.draft = draft
                conversation.save(update_fields=["draft", "updated_at"])
        if created:
            record_conversation_event(
                conversation=conversation,
                event_code="CONVERSATION_STARTED",
                event_key=f"conversation:{conversation.id}:started",
            )
    return conversation, created


def _direct_key(user_id, target_user_id):
    left, right = sorted((int(user_id), int(target_user_id)))
    return f"{left}:{right}"


def start_direct_conversation(*, user, target_user=None, artisan_id=None):
    """Create or resume a generic two-user direct conversation.

    This deliberately does not populate customer/artisan/service_request because a direct
    chat is not a marketplace service workflow. Marketplace service conversations continue
    to use start_conversation().
    """
    if not user.is_authenticated:
        raise PermissionDenied("Authentication is required.")

    if artisan_id is not None:
        try:
            artisan = ArtisanProfile.objects.select_related("user", "verification").get(
                pk=artisan_id
            )
        except ArtisanProfile.DoesNotExist as exc:
            raise NotFound("Artisan not found.") from exc
        if artisan.user_id == user.id:
            raise PermissionDenied(
                "You cannot start a direct conversation with yourself."
            )
        if (
            not artisan.user.is_active
            or artisan.profile_status != "ACTIVE"
            or getattr(artisan, "verification", None) is None
            or artisan.verification.verification_status != "VERIFIED"
        ):
            raise PermissionDenied(
                "The artisan is not currently available for direct messaging."
            )
        target_user = artisan.user

    if target_user is None:
        raise ValidationError({"participant": "A target participant is required."})
    if target_user.id == user.id:
        raise ValidationError(
            {"participants": "You cannot start a conversation with yourself."}
        )
    if not target_user.is_active:
        raise PermissionDenied("The selected user is not active.")
    if getattr(target_user, "is_staff", False):
        raise PermissionDenied(
            "Staff conversations must be opened through support workflows."
        )

    target_artisan = getattr(target_user, "artisan_profile", None)
    sender_artisan = getattr(user, "artisan_profile", None)
    if target_artisan and hasattr(user, "customer_profile"):
        return start_conversation(user=user, artisan_id=target_artisan.pk)
    if (
        sender_artisan
        and hasattr(target_user, "customer_profile")
        and not target_artisan
    ):
        return start_conversation(user=target_user, artisan_id=sender_artisan.pk)
    key = _direct_key(user.id, target_user.id)
    with transaction.atomic():
        conversation = (
            Conversation.objects.select_for_update(of=("self",))
            .filter(
                conversation_type=Conversation.ConversationType.DIRECT,
                direct_key=key,
            )
            .first()
        )
        created = False
        if conversation is None:
            try:
                conversation = Conversation.objects.create(
                    conversation_type=Conversation.ConversationType.DIRECT,
                    direct_key=key,
                )
                conversation.participants.set([user, target_user])
                created = True
            except IntegrityError:
                conversation = Conversation.objects.select_for_update(of=("self",)).get(
                    conversation_type=Conversation.ConversationType.DIRECT,
                    direct_key=key,
                )
        else:
            conversation.participants.set([user, target_user])
        if created:
            record_conversation_event(
                conversation=conversation,
                event_code="CONVERSATION_STARTED",
                event_key=f"conversation:{conversation.id}:started",
                metadata={"conversation_type": Conversation.ConversationType.DIRECT},
            )
    return conversation, created


def ensure_participants(conversation):
    ids = []
    if conversation.customer_id:
        ids.append(conversation.customer_id)
    if (
        conversation.artisan_id
        and conversation.artisan
        and conversation.artisan.user_id
    ):
        ids.append(conversation.artisan.user_id)
    if ids:
        conversation.participants.set(ids)
    return ids


def send_message(*, conversation, user, body, attachments=None, idempotency_key=None):
    participant = conversation.participants.filter(pk=user.id).exists()
    legacy_participant = conversation.customer_id == user.id or (
        conversation.artisan_id and conversation.artisan.user_id == user.id
    )
    if not participant and not legacy_participant:
        raise PermissionDenied("You are not a participant in this conversation.")
    if not participant:
        ensure_participants(conversation)
    body = str(body or "").strip()
    attachments = list(attachments or [])
    if not body and not attachments:
        raise ValidationError(
            {"body": "Message body or at least one attachment is required."}
        )
    if len(body) > 5000:
        raise ValidationError({"body": "Message is too long."})
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValidationError(
            {
                "attachments": f"At most {MAX_ATTACHMENTS_PER_MESSAGE} files are allowed per message."
            }
        )
    key = str(idempotency_key).strip() if idempotency_key is not None else None
    if key is not None and (not key or len(key) > 255):
        raise ValidationError(
            {"Idempotency-Key": "Idempotency-Key must be 1 to 255 characters."}
        )
    if key:
        existing = Message.objects.filter(
            conversation=conversation, sender=user, idempotency_key=key
        ).first()
        if existing:
            existing_count = existing.attachments.count()
            if existing.body != body or existing_count != len(attachments):
                raise ValidationError(
                    {
                        "Idempotency-Key": "The idempotency key was already used for a different message payload."
                    }
                )
            return existing
    validated_attachments = [
        (upload, validate_attachment(upload)) for upload in attachments
    ]
    has_recording = any(
        mime.startswith(("audio/", "video/")) for _, mime in validated_attachments
    )
    if has_recording and (
        conversation.conversation_type != "SERVICE"
        or not conversation.service_request_id
    ):
        raise ValidationError(
            {"attachments": "Recordings require a request-linked service room."}
        )
    with transaction.atomic():
        if has_recording:
            from .retention import eligible
            from .models import MediaPurge

            req = ServiceRequest.objects.select_for_update().get(
                pk=conversation.service_request_id
            )
            if (
                req.status == "CLOSED"
                or eligible(req)
                or MediaPurge.objects.filter(
                    service_request=req, blocked_at__isnull=False
                ).exists()
            ):
                raise ValidationError(
                    {
                        "attachments": "Recording uploads are closed for this completed service."
                    }
                )
        try:
            with transaction.atomic():
                message = Message.objects.create(
                    conversation=conversation,
                    sender=user,
                    message_type=Message.MessageType.USER,
                    body=body,
                    idempotency_key=key,
                )
        except IntegrityError:
            if not key:
                raise
            existing = Message.objects.filter(
                conversation=conversation, sender=user, idempotency_key=key
            ).first()
            if not existing:
                raise
            if existing.body != body or existing.attachments.count() != len(
                attachments
            ):
                raise ValidationError(
                    {
                        "Idempotency-Key": "The idempotency key was already used for a different message payload."
                    }
                ) from None
            return existing
        from core.services import enqueue_outbox

        for upload, content_type in validated_attachments:
            attachment = ConversationAttachment.objects.create(
                message=message,
                file=upload,
                content_type=content_type,
                size=upload.size,
            )
            enqueue_outbox(
                event_type="media.scan",
                event_key=f"media-scan:{attachment.id}",
                payload={"attachment_id": str(attachment.id)},
            )
        conversation.save(update_fields=["updated_at"])
        from .notifications import notify_room_message

        notify_room_message(message)
        transaction.on_commit(
            lambda: __import__(
                "core.tasks", fromlist=["dispatch_outbox_events"]
            ).dispatch_outbox_events.delay(),
            robust=True,
        )
    try:
        from .automation import apply_message_automation

        updated_conversation, _service_request = apply_message_automation(
            conversation=conversation, user=user, body=body
        )
        conversation = updated_conversation
    except Exception:
        logger.exception("message_automation_failed conversation=%s", conversation.id)
    if (
        conversation.service_request_id
        and conversation.artisan_id
        and user.id == conversation.artisan.user_id
    ):
        booking = _booking_for_conversation(conversation)
        if booking and booking.status == Booking.Status.IN_PROGRESS:
            record_conversation_event(
                conversation=conversation,
                event_code="SERVICE_PROGRESS_UPDATED",
                event_key=f"message:{message.id}:progress",
                metadata={"message_id": str(message.id)},
            )
    return message


def get_current_offer(conversation, *, lock=False):
    queryset = conversation.offers.filter(
        status__in=[Offer.Status.PROPOSED, Offer.Status.REVISION_REQUESTED]
    ).order_by("-created_at")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    offer = queryset.first()
    if offer and offer.expires_at <= timezone.now():
        return None
    return offer


def _validate_offer_common(*, conversation, user, payload, parent_offer=None):
    if not conversation.request_submitted or not conversation.artisan_id:
        raise ValidationError("Complete the service request before creating an offer.")
    if conversation.customer_id != user.id and conversation.artisan.user_id != user.id:
        raise PermissionDenied("You are not a participant in this conversation.")
    try:
        labour = Decimal(str(payload.get("labour_amount"))).quantize(Decimal("0.01"))
        materials = Decimal(str(payload.get("materials_amount"))).quantize(
            Decimal("0.01")
        )
        total = Decimal(str(payload.get("total_amount"))).quantize(Decimal("0.01"))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ValidationError(
            {"amount": "Offer amounts must be valid decimal amounts."}
        ) from exc
    if not all(amount.is_finite() for amount in (labour, materials, total)):
        raise ValidationError(
            {"amount": "Offer amounts must be finite decimal values."}
        )
    if labour < 0 or materials < 0:
        raise ValidationError("Labour and materials amounts cannot be negative.")
    if total <= 0:
        raise ValidationError(
            {"total_amount": "Total amount must be greater than zero."}
        )
    for term in ("payment_terms", "exclusions", "workmanship_terms"):
        if not str(payload.get(term, "")).strip():
            raise ValidationError(
                {term: "State this term explicitly, or explicitly state None."}
            )
    if materials and not str(payload.get("materials_details", "")).strip():
        raise ValidationError(
            {"materials_details": "Disclose the materials covered by this price."}
        )
    transport = Decimal(str(payload.get("transport_amount", 0))).quantize(
        Decimal("0.01")
    )
    additional = Decimal(str(payload.get("additional_amount", 0))).quantize(
        Decimal("0.01")
    )
    if (
        not transport.is_finite()
        or not additional.is_finite()
        or transport < 0
        or additional < 0
    ):
        raise ValidationError("Extra charges must be finite and nonnegative.")
    if additional and not str(payload.get("additional_charge_details", "")).strip():
        raise ValidationError("Disclose what each additional charge covers.")
    if labour + materials + transport + additional != total:
        raise ValidationError(
            {
                "total_amount": "Labour, materials, transport and disclosed additional charges must equal total."
            }
        )
    if total != total.quantize(Decimal("1")):
        raise ValidationError(
            {
                "total_amount": "Offer totals must be whole NGN amounts for the current Flutterwave payment integration."
            }
        )
    if int(total * 100) > getattr(settings, "MAX_SERVICE_AMOUNT_KOBO", 100_000_000_000):
        raise ValidationError(
            {"total_amount": "Offer exceeds the platform transaction limit."}
        )
    try:
        scheduled_start = payload["scheduled_start"]
        scheduled_end = payload["scheduled_end"]
        expires_at = payload["expires_at"]
    except KeyError as exc:
        raise ValidationError({str(exc): "This field is required."}) from exc
    if (
        scheduled_start.tzinfo is None
        or scheduled_end.tzinfo is None
        or expires_at.tzinfo is None
    ):
        raise ValidationError("Offer datetimes must be timezone-aware.")
    now = timezone.now()
    if scheduled_start < now or scheduled_end <= scheduled_start:
        raise ValidationError(
            {"scheduled_start": "Schedule must be in the future and end after start."}
        )
    if expires_at <= now:
        raise ValidationError({"expires_at": "Offer expiry must be in the future."})
    if expires_at > scheduled_start:
        raise ValidationError(
            {"expires_at": "Offer expiry must be on or before the scheduled start."}
        )
    scope = str(payload.get("scope", "")).strip()
    if not scope or len(scope) > 5000:
        raise ValidationError(
            {"scope": "Offer scope is required and must be 5,000 characters or fewer."}
        )
    notes = str(payload.get("notes", "")).strip()
    if len(notes) > 3000:
        raise ValidationError({"notes": "Offer notes are too long."})
    if parent_offer:
        if (
            parent_offer.conversation_id != conversation.id
            or parent_offer.status
            not in {Offer.Status.PROPOSED, Offer.Status.REVISION_REQUESTED}
            or parent_offer.expires_at <= now
        ):
            raise ValidationError(
                {"parent_offer_id": "The parent offer is no longer active."}
            )
        if (
            parent_offer.created_by_id == user.id
            and parent_offer.status != Offer.Status.REVISION_REQUESTED
        ):
            raise PermissionDenied(
                "A counter offer must be created by the other participant."
            )
    else:
        if user.id != conversation.artisan.user_id:
            raise PermissionDenied("Only the artisan can create the initial offer.")
        if get_current_offer(conversation):
            raise ValidationError(
                "An active offer already exists. Counter or decline it instead."
            )
    return (
        labour,
        materials,
        total,
        scheduled_start,
        scheduled_end,
        expires_at,
        scope,
        notes,
    )


def create_offer(
    *, conversation, user, payload, parent_offer_id=None, idempotency_key=None
):
    key = str(idempotency_key).strip() if idempotency_key is not None else None
    if key is not None and (not key or len(key) > 255):
        raise ValidationError(
            {"Idempotency-Key": "Idempotency-Key must be 1 to 255 characters."}
        )
    with transaction.atomic():
        if conversation.service_request_id:
            ServiceRequest.objects.select_for_update().get(
                pk=conversation.service_request_id
            )
        conversation = (
            Conversation.objects.select_for_update(of=("self",))
            .select_related("artisan__user", "customer")
            .get(pk=conversation.pk)
        )
        if key:
            existing = Offer.objects.filter(
                created_by=user, idempotency_key=key
            ).first()
            if existing:
                if (
                    existing.payload_hash
                    and existing.payload_hash != quote_payload_hash(payload)
                ):
                    raise ValidationError(
                        "This offer key was already used for different terms."
                    )
                if existing.conversation_id != conversation.id:
                    raise ValidationError(
                        "This idempotency key is already associated with another conversation."
                    )
                normalized_existing_parent = (
                    str(existing.parent_offer_id)
                    if existing.parent_offer_id is not None
                    else None
                )
                requested_parent_id = (
                    str(parent_offer_id) if parent_offer_id is not None else None
                )
                if normalized_existing_parent != requested_parent_id:
                    raise ValidationError(
                        "This idempotency key was already used for a different offer operation."
                    )
                return existing
        parent = None
        if parent_offer_id:
            try:
                parent = Offer.objects.select_for_update(of=("self",)).get(
                    pk=parent_offer_id
                )
            except Offer.DoesNotExist as exc:
                raise NotFound("Parent offer not found.") from exc
        values = _validate_offer_common(
            conversation=conversation, user=user, payload=payload, parent_offer=parent
        )
        (
            labour,
            materials,
            total,
            scheduled_start,
            scheduled_end,
            expires_at,
            scope,
            notes,
        ) = values
        if parent:
            parent.status = Offer.Status.COUNTERED
            parent.save(update_fields=["status", "updated_at"])
        try:
            with transaction.atomic():
                from django.db.models import Max

                version = (
                    Offer.objects.filter(conversation=conversation).aggregate(
                        value=Max("version")
                    )["value"]
                    or 0
                ) + 1
                offer = Offer.objects.create(
                    version=version,
                    materials_details=payload.get("materials_details", ""),
                    conversation=conversation,
                    created_by=user,
                    parent_offer=parent,
                    labour_amount=labour,
                    materials_amount=materials,
                    total_amount=total,
                    materials_included=bool(payload.get("materials_included", False)),
                    scope=scope,
                    notes=notes,
                    payload_hash=quote_payload_hash(payload),
                    transport_amount=payload.get("transport_amount", 0),
                    additional_amount=payload.get("additional_amount", 0),
                    additional_charge_details=payload.get(
                        "additional_charge_details", ""
                    ),
                    payment_terms=payload.get("payment_terms", ""),
                    exclusions=payload.get("exclusions", ""),
                    workmanship_terms=payload.get("workmanship_terms", ""),
                    scheduled_start=scheduled_start,
                    scheduled_end=scheduled_end,
                    expires_at=expires_at,
                    idempotency_key=key,
                )
        except IntegrityError as exc:
            if idempotency_key:
                existing = Offer.objects.filter(
                    created_by=user, idempotency_key=key
                ).first()
                if existing:
                    if (
                        existing.payload_hash
                        and existing.payload_hash != quote_payload_hash(payload)
                    ):
                        raise ValidationError(
                            "This offer key was already used for different terms."
                        )
                    normalized_existing_parent = (
                        str(existing.parent_offer_id)
                        if existing.parent_offer_id is not None
                        else None
                    )
                    requested_parent_id = (
                        str(parent_offer_id) if parent_offer_id is not None else None
                    )
                    if (
                        existing.conversation_id != conversation.id
                        or normalized_existing_parent != requested_parent_id
                    ):
                        raise ValidationError(
                            "This idempotency key was already used for a different offer operation."
                        ) from exc
                    return existing
            raise ValidationError(
                "Unable to create offer due to a concurrent update."
            ) from exc
        record_conversation_event(
            conversation=conversation,
            event_code="OFFER_COUNTERED" if parent else "OFFER_CREATED",
            event_key=f"offer:{offer.id}:created",
            metadata={
                "offer_id": str(offer.id),
                "total_amount": str(total),
                "currency": "NGN",
            },
            body=(
                f"Offer created for {money_text(int(total * 100))}."
                if not parent
                else f"Counter offer created for {money_text(int(total * 100))}."
            ),
        )
        conversation.save(update_fields=["updated_at"])
    return offer


def decline_offer(*, conversation, user, offer_id):
    with transaction.atomic():
        if conversation.service_request_id:
            ServiceRequest.objects.select_for_update().get(
                pk=conversation.service_request_id
            )
        conversation = (
            Conversation.objects.select_for_update(of=("self",))
            .select_related("artisan__user", "customer")
            .get(pk=conversation.pk)
        )
        try:
            offer = Offer.objects.select_for_update(of=("self",)).get(
                pk=offer_id, conversation=conversation
            )
        except Offer.DoesNotExist as exc:
            raise NotFound("Offer not found.") from exc
        if conversation.customer_id != user.id:
            raise PermissionDenied("Only the customer can decline an offer.")
        if offer.created_by_id == user.id:
            raise PermissionDenied("You cannot decline your own offer.")
        if offer.status != Offer.Status.PROPOSED or offer.expires_at <= timezone.now():
            raise ValidationError("Only the current active offer can be declined.")
        offer.status = Offer.Status.DECLINED
        offer.save(update_fields=["status", "updated_at"])
        record_conversation_event(
            conversation=conversation,
            event_code="OFFER_DECLINED",
            event_key=f"offer:{offer.id}:declined",
            metadata={"offer_id": str(offer.id)},
        )
        conversation.save(update_fields=["updated_at"])
    return offer


def accept_offer(*, conversation, user, offer_id):
    if conversation.customer_id != user.id:
        raise PermissionDenied("Only the customer can accept an artisan offer.")
    with transaction.atomic():
        if conversation.service_request_id:
            ServiceRequest.objects.select_for_update().get(
                pk=conversation.service_request_id
            )
        conversation = (
            Conversation.objects.select_for_update(of=("self",))
            .select_related(
                "customer",
                "artisan__user",
                "service_request__category",
                "service_request__location",
            )
            .get(pk=conversation.pk)
        )
        offer = (
            Offer.objects.select_for_update(of=("self",))
            .filter(pk=offer_id, conversation=conversation)
            .first()
        )
        if offer is None:
            raise NotFound("Offer not found.")
        if offer.status == Offer.Status.ACCEPTED:
            existing_booking = (
                Booking.objects.filter(request_id=conversation.service_request_id)
                .select_related("customer__user", "artisan__user")
                .order_by("-created_at")
                .first()
                if conversation.service_request_id
                else None
            )
            if (
                existing_booking
                and existing_booking.customer.user_id
                == conversation.service_request.requester_id
            ):
                existing_payment = getattr(existing_booking, "escrow_payment", None)
                return existing_booking, existing_payment, offer
            raise ValidationError("This offer has already been accepted.")
        if offer.created_by_id == user.id:
            raise PermissionDenied("You cannot accept your own offer.")
        if offer.status != Offer.Status.PROPOSED or offer.expires_at <= timezone.now():
            raise ValidationError("This offer is no longer acceptable.")
        latest = (
            Offer.objects.select_for_update(of=("self",))
            .filter(conversation=conversation, status=Offer.Status.PROPOSED)
            .order_by("-created_at")
            .first()
        )
        if latest is None or latest.id != offer.id:
            raise ValidationError("Only the current active offer can be accepted.")
        service_request = conversation.service_request
        if service_request is None:
            raise ValidationError(
                "Complete the service request before accepting an offer."
            )
        if service_request.status != ServiceRequest.Status.OPEN:
            raise ValidationError("This request is no longer open for booking.")
        artisan = conversation.artisan
        if (
            artisan.profile_status != "ACTIVE"
            or not artisan.user.is_active
            or getattr(artisan, "verification", None) is None
            or artisan.verification.verification_status != "VERIFIED"
        ):
            raise PermissionDenied("The artisan is no longer eligible for booking.")
        if (
            not ArtisanService.objects.filter(
                artisan=artisan,
                service__category=service_request.category,
                service__is_active=True,
            )
            .filter(Q(location=service_request.location) | Q(location__isnull=True))
            .exists()
        ):
            raise PermissionDenied(
                "The artisan no longer provides this service in the requested location."
            )
        if Booking.objects.filter(
            request=service_request,
            status__in=[
                Booking.Status.REQUESTED,
                Booking.Status.ACCEPTED,
                Booking.Status.IN_PROGRESS,
                Booking.Status.COMPLETION_REQUESTED,
                Booking.Status.DISPUTED,
            ],
        ).exists():
            raise ValidationError("This service request already has an active booking.")
        customer_profile = CustomerProfile.objects.filter(user=user).first()
        if not customer_profile:
            raise PermissionDenied("A customer profile is required before booking.")
        booking_created = True
        try:
            with transaction.atomic():
                booking = Booking.objects.create(
                    request=service_request,
                    artisan=artisan,
                    customer=customer_profile,
                    scheduled_time_start=offer.scheduled_start,
                    scheduled_time_end=offer.scheduled_end,
                    price_agreed=offer.total_amount,
                    status=Booking.Status.ACCEPTED,
                    source="WEB",
                )
        except IntegrityError:
            booking_created = False
            booking = (
                Booking.objects.filter(
                    request=service_request,
                    status__in=[
                        Booking.Status.REQUESTED,
                        Booking.Status.ACCEPTED,
                        Booking.Status.IN_PROGRESS,
                        Booking.Status.COMPLETION_REQUESTED,
                        Booking.Status.DISPUTED,
                    ],
                )
                .select_related("customer__user", "artisan__user")
                .order_by("-created_at")
                .first()
            )
            if (
                booking is None
                or booking.customer_id != customer_profile.id
                or booking.artisan_id != artisan.id
            ):
                raise ValidationError(
                    "This service request already has an active booking."
                ) from None
            existing_offer = (
                Offer.objects.filter(
                    conversation=conversation, status=Offer.Status.ACCEPTED
                )
                .order_by("-updated_at")
                .first()
            )
            if existing_offer:
                offer = existing_offer
            existing_payment = getattr(booking, "escrow_payment", None)
            if existing_payment:
                return booking, existing_payment, offer
        if booking_created:
            BookingStatusHistory.objects.create(
                booking=booking,
                status=Booking.Status.ACCEPTED,
                changed_by="CUSTOMER",
                changed_by_user=user,
                notes="Booking created automatically from accepted offer.",
            )
            # The request has been attended to: it leaves the customer's request
            # list, but the row stays for bookings, history and reporting.
            service_request.mark_attended()
        offer.status = Offer.Status.ACCEPTED
        offer.save(update_fields=["status", "updated_at"])
        Offer.objects.filter(
            conversation=conversation, status=Offer.Status.PROPOSED
        ).exclude(pk=offer.pk).update(status=Offer.Status.CANCELLED)
        payment = prepare_booking_payment(booking=booking)
        record_conversation_event(
            conversation=conversation,
            event_code="OFFER_ACCEPTED",
            event_key=f"offer:{offer.id}:accepted",
            metadata={
                "offer_id": str(offer.id),
                "total_amount": str(offer.total_amount),
                "currency": "NGN",
            },
        )
        record_conversation_event(
            conversation=conversation,
            event_code="BOOKING_CREATED",
            event_key=f"booking:{booking.id}:created",
            metadata={"booking_id": str(booking.id)},
        )
        record_conversation_event(
            conversation=conversation,
            event_code="BOOKING_ACCEPTED",
            event_key=f"booking:{booking.id}:accepted",
            metadata={"booking_id": str(booking.id)},
        )
        record_conversation_event(
            conversation=conversation,
            event_code="PAYMENT_PENDING",
            event_key=f"payment:{payment.id}:pending",
            metadata={
                "payment_id": str(payment.id),
                "amount_kobo": payment.total_amount_kobo,
                "currency": "NGN",
            },
        )
        transaction.on_commit(
            lambda payment_id=str(payment.id): __import__(
                "payments.tasks", fromlist=["initialize_prepared_payment"]
            ).initialize_prepared_payment.delay(payment_id),
            robust=True,
        )
        conversation.save(update_fields=["updated_at"])
    return booking, payment, offer


def expire_offers(*, batch_size=500):
    now = timezone.now()
    expired = []
    ids = list(
        Offer.objects.filter(
            status=Offer.Status.PROPOSED, expires_at__lte=now
        ).values_list("id", flat=True)[:batch_size]
    )
    for offer_id in ids:
        with transaction.atomic():
            offer = (
                Offer.objects.select_for_update(of=("self",))
                .filter(
                    pk=offer_id,
                    status=Offer.Status.PROPOSED,
                    expires_at__lte=timezone.now(),
                )
                .select_related("conversation")
                .first()
            )
            if not offer:
                continue
            offer.status = Offer.Status.EXPIRED
            offer.save(update_fields=["status", "updated_at"])
            record_conversation_event(
                conversation=offer.conversation,
                event_code="OFFER_EXPIRED",
                event_key=f"offer:{offer.id}:expired",
                metadata={"offer_id": str(offer.id)},
            )
            expired.append(str(offer.id))
    return expired


def mark_conversation_read(*, conversation, user, last_message_id=None):
    seen_at = timezone.now()
    if last_message_id:
        seen = Message.objects.filter(
            conversation=conversation, pk=last_message_id
        ).first()
        if not seen:
            raise ValidationError(
                "The read marker must identify a message in this conversation."
            )
        seen_at = seen.created_at
    with transaction.atomic():
        state, _ = ConversationReadState.objects.select_for_update().get_or_create(
            conversation=conversation, user=user
        )
        if state.last_read_at is None or seen_at > state.last_read_at:
            state.last_read_at = seen_at
            state.save(update_fields=["last_read_at", "updated_at"])
    return (
        Message.objects.filter(
            conversation=conversation, created_at__gt=state.last_read_at
        )
        .exclude(sender=user)
        .count()
    )


def open_conversation_dispute(*, conversation, user, reason):
    if (
        conversation.conversation_type != Conversation.ConversationType.SERVICE
        or conversation.customer_id != user.id
    ):
        raise PermissionDenied(
            "Only the customer in this service conversation can dispute its booking."
        )
    if not conversation.service_request_id or not conversation.artisan_id:
        raise ValidationError("There is no booking to dispute.")
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("request", "customer__user", "artisan__user")
            .filter(
                request_id=conversation.service_request_id,
                customer__user_id=conversation.customer_id,
                artisan_id=conversation.artisan_id,
                request__requester_id=conversation.customer_id,
            )
            .filter(
                status__in=[
                    Booking.Status.ACCEPTED,
                    Booking.Status.IN_PROGRESS,
                    Booking.Status.COMPLETION_REQUESTED,
                ]
            )
            .first()
        )
        if not booking:
            raise ValidationError(
                "This booking cannot be disputed in its current state."
            )
        existing_case = SupportCase.objects.filter(
            booking=booking,
            case_type=SupportCase.CaseType.DISPUTE,
            status__in=[SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW],
        ).first()
        if existing_case:
            raise ValidationError("An active dispute already exists for this booking.")
        from core.state import BOOKING_TRANSITIONS, require_transition

        booking_status_before_dispute = booking.status
        require_transition(
            BOOKING_TRANSITIONS,
            booking.status,
            Booking.Status.DISPUTED,
            resource="booking",
        )
        booking.status = Booking.Status.DISPUTED
        booking.save(update_fields=["status", "updated_at"])
        try:
            from payments.models import EscrowPayment

            payments = (
                EscrowPayment.objects.select_for_update(of=("self",))
                .filter(booking=booking)
                .order_by("pk")
            )
            for payment in payments:
                if payment.status in [
                    EscrowPayment.Status.HELD,
                    EscrowPayment.Status.RELEASE_PENDING,
                ]:
                    payment.status = EscrowPayment.Status.DISPUTED
                    payment.failure_reason = reason
                    payment.save(
                        update_fields=["status", "failure_reason", "updated_at"]
                    )
        except ImportError:
            pass
        BookingStatusHistory.objects.create(
            booking=booking,
            status=Booking.Status.DISPUTED,
            changed_by="CUSTOMER",
            changed_by_user=user,
            notes=reason,
        )
        from django.db import IntegrityError

        try:
            with transaction.atomic():
                case = create_support_case(
                    opened_by=user,
                    booking=booking,
                    conversation=conversation,
                    case_type=SupportCase.CaseType.DISPUTE,
                    priority=SupportCase.Priority.HIGH,
                    subject=f"Booking dispute: {booking.pk}",
                    description=reason,
                    booking_status_before_dispute=booking_status_before_dispute,
                )
        except IntegrityError as exc:
            existing_case = SupportCase.objects.filter(
                booking=booking,
                case_type=SupportCase.CaseType.DISPUTE,
                status__in=[SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW],
            ).first()
            if existing_case:
                raise ValidationError(
                    "An active dispute already exists for this booking."
                ) from exc
            raise
        record_conversation_event(
            conversation=conversation,
            event_code="DISPUTE_OPENED",
            event_key=f"booking:{booking.id}:disputed",
            metadata={"booking_id": str(booking.id), "case_id": case.id},
        )
        conversation.save(update_fields=["updated_at"])
    return booking, case


def _booking_for_conversation(conversation):
    if (
        conversation.conversation_type != Conversation.ConversationType.SERVICE
        or not conversation.service_request_id
        or conversation.service_request.status in ("DRAFT", "CLOSED")
        or not conversation.customer_id
        or not conversation.artisan_id
    ):
        return None
    return (
        Booking.objects.filter(
            request_id=conversation.service_request_id,
            customer__user_id=conversation.customer_id,
            artisan_id=conversation.artisan_id,
            request__requester_id=conversation.customer_id,
        )
        .select_related("request", "customer__user", "artisan__user")
        .order_by("-created_at")
        .first()
    )


def _next_question(conversation):
    if (
        conversation.service_request_id
        and conversation.service_request.status == "CLOSED"
    ):
        return None
    if (
        conversation.conversation_type != Conversation.ConversationType.SERVICE
        or conversation.request_submitted
    ):
        return None
    draft = conversation.draft or {}
    if not draft.get("service_id"):
        category_id = draft.get("category_id")
        if category_id:
            category_name = (
                ServiceCategory.objects.filter(pk=category_id)
                .values_list("name", flat=True)
                .first()
            )
            if category_name:
                return f"Which listed service in {category_name} do you need?"
        return (
            "What service do you need? Choose from this artisan’s available services."
        )
    if not draft.get("description"):
        return "What do you need done, or what problem are you experiencing?"
    if not draft.get("location_id"):
        if draft.get("state"):
            return f"Which area in {draft['state']} should the service be carried out? Choose it from the location catalogue."
        if draft.get("country"):
            return f"Which state or region and area in {draft['country']} should the service be carried out?"
        return "Where should the service be carried out? Choose a location from the catalogue."
    if not draft.get("preferred_date"):
        return "What date would you prefer?"
    return None


def _available_actions(*, conversation, user, booking=None, offer=None):
    now = timezone.now()
    actions = ["SEND_MESSAGE"]
    if conversation.conversation_type == Conversation.ConversationType.DIRECT:
        return actions if conversation.participants.filter(pk=user.id).exists() else []
    is_customer = conversation.customer_id == user.id
    is_artisan = bool(
        conversation.artisan_id and conversation.artisan.user_id == user.id
    )
    if not is_customer and not is_artisan:
        return []
    if (
        conversation.service_request_id
        and conversation.service_request.status == "CLOSED"
    ):
        return actions
    if not booking:
        if is_artisan:
            if offer and offer.status == Offer.Status.REVISION_REQUESTED:
                actions.append("REVISE_OFFER")
            if (
                offer
                and offer.created_by_id == conversation.customer_id
                and offer.status == Offer.Status.PROPOSED
                and offer.expires_at > now
            ):
                actions.append("COUNTER_OFFER")
            elif not offer and conversation.request_submitted:
                actions.append("SEND_OFFER")
        if (
            is_customer
            and offer
            and conversation.artisan_id
            and offer.created_by_id == conversation.artisan.user_id
            and offer.status == Offer.Status.PROPOSED
            and offer.expires_at > now
        ):
            actions.extend(["ACCEPT_OFFER", "DECLINE_OFFER", "COUNTER_OFFER"])
        if is_customer and not conversation.request_submitted:
            actions.append("EDIT_REQUEST")
        return actions
    if booking.status == Booking.Status.ACCEPTED:
        if (
            is_customer
            and getattr(booking, "escrow_payment", None)
            and booking.escrow_payment.status == "PAYMENT_PENDING"
        ):
            actions.append("PAY")
        if (
            is_artisan
            and getattr(booking, "escrow_payment", None)
            and booking.escrow_payment.status == "HELD"
        ):
            actions.append("START_BOOKING")
    if booking.status == Booking.Status.IN_PROGRESS and is_artisan:
        # Keep the completion action out of the conversation UI until every open add-on
        # negotiation is resolved and every approved add-on has provider-confirmed escrow.
        from booking.additional_orders import approved_addon_funding
        from booking.models import BookingAdditionalOrder

        has_open_addon = BookingAdditionalOrder.objects.filter(
            booking=booking, status=BookingAdditionalOrder.Status.NEGOTIATING
        ).exists()
        all_approved_addons_funded, _funded_payments, _unpaid_orders = (
            approved_addon_funding(booking)
        )
        if not has_open_addon and all_approved_addons_funded:
            actions.append("SUBMIT_COMPLETION")
    if booking.status == Booking.Status.COMPLETION_REQUESTED and is_customer:
        actions.extend(["CONFIRM_COMPLETION", "DISPUTE"])
    if (
        booking.status
        in {
            Booking.Status.ACCEPTED,
            Booking.Status.IN_PROGRESS,
            Booking.Status.COMPLETION_REQUESTED,
        }
        and is_customer
    ):
        if "DISPUTE" not in actions:
            actions.append("DISPUTE")
        actions.append("CANCEL")
    if booking.status == Booking.Status.COMPLETED and is_customer:
        from reviews.models import Review

        if not Review.objects.filter(booking=booking).exists():
            actions.append("REVIEW")
    return list(dict.fromkeys(actions))


def hydrate_conversation(conversation, *, user, unread_count=False):
    booking = getattr(conversation, "_booking", None) or _booking_for_conversation(
        conversation
    )
    offer = getattr(conversation, "_active_offer", None) or get_current_offer(
        conversation
    )
    conversation._booking = booking
    conversation._active_offer = offer
    payment = getattr(booking, "escrow_payment", None) if booking else None
    conversation._conversation_state = derive_conversation_state(
        conversation=conversation, booking=booking, offer=offer, payment=payment
    )
    conversation._available_actions = _available_actions(
        conversation=conversation, user=user, booking=booking, offer=offer
    )
    conversation._next_question = _next_question(conversation)
    read_state = getattr(conversation, "_read_state", None)
    unread_qs = Message.objects.filter(conversation=conversation).exclude(sender=user)
    if read_state and read_state.last_read_at:
        unread_qs = unread_qs.filter(created_at__gt=read_state.last_read_at)
    conversation._unread_count = (
        unread_qs.count() if unread_count else int(unread_qs.exists())
    )
    conversation._unread = bool(conversation._unread_count)
    conversation._last_message_at = (
        Message.objects.filter(conversation=conversation)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    return conversation


def start_category_request(*, user, category_id=None, service_id=None):
    if not hasattr(user, "customer_profile"):
        raise PermissionDenied("A customer profile is required.")
    service = (
        Service.objects.select_related("category")
        .filter(pk=service_id, is_active=True)
        .first()
        if service_id
        else None
    )
    if service_id and not service:
        raise ValidationError({"service_id": "Choose an active service."})
    category = (
        service.category
        if service
        else ServiceCategory.objects.filter(pk=category_id).first()
    )
    if not category:
        raise ValidationError({"category_id": "Choose a real service category."})
    # Lock the owner, not presence state, to serialize identical draft starts.
    from django.contrib.auth import get_user_model

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        import hashlib

        key = (
            "category-draft:"
            + hashlib.sha256(
                f'{user.pk}:{category.pk}:{service.pk if service else "all"}'.encode()
            ).hexdigest()
        )
        room = (
            Conversation.objects.select_for_update()
            .filter(direct_key=key, service_request__status="DRAFT")
            .first()
        )
        created = room is None
        if created:
            room = Conversation.objects.create(
                conversation_type="SERVICE",
                customer=user,
                direct_key=key,
                draft={
                    "category_id": str(category.pk),
                    **({"service_id": str(service.pk)} if service else {}),
                },
            )
            room.participants.add(user)
            record_conversation_event(
                conversation=room,
                event_code="CONVERSATION_STARTED",
                event_key=f"conversation:{room.pk}:started",
                body="Hello! What do you need help with today? Describe the problem, send a picture or record a voice note. This is a draft until you review it and choose Send Request.",
            )
        return room, created


def claim_request_room(*, user, request_id):
    artisan = getattr(user, "artisan_profile", None)
    if not artisan:
        raise PermissionDenied("Only an eligible artisan can respond.")
    with transaction.atomic():
        get_artisan_or_403(user=user, artisan_id=artisan.pk, allow_self=True)
        req = (
            ServiceRequest.objects.select_for_update()
            .filter(pk=request_id, status="OPEN")
            .first()
        )
        if not req:
            raise NotFound("An open service request was not found.")
        if req.requester_id == user.pk:
            raise PermissionDenied("You cannot respond to your own request.")
        if not req.service_id:
            raise ValidationError(
                "The customer must select a service before submission."
            )
        validate_service_for_artisan(artisan=artisan, service_id=req.service_id)
        validate_artisan_location(
            artisan=artisan, service=req.service, location_id=req.location_id
        )
        room = (
            Conversation.objects.select_for_update().filter(service_request=req).first()
        )
        if room and room.artisan_id and room.artisan_id != artisan.pk:
            raise ValidationError("This request already has an assigned service room.")
        if room is None:
            room = Conversation.objects.create(
                conversation_type="SERVICE",
                customer_id=req.requester_id,
                artisan=artisan,
                service_request=req,
            )
        else:
            room.artisan = artisan
            room.save(update_fields=["artisan", "updated_at"])
        room.participants.set([req.requester_id, user.pk])
        record_conversation_event(
            conversation=room,
            event_code="ARTISAN_RESPONDED",
            event_key=f"request:{req.pk}:artisan:{artisan.pk}:responded",
            body="An eligible artisan has joined this request to discuss the work and prepare a formal quote.",
        )
        return room, False


def request_quote_revision(*, conversation, user, offer_id):
    if conversation.customer_id != user.pk:
        raise PermissionDenied("Only the customer may request a quote revision.")
    with transaction.atomic():
        offer = (
            Offer.objects.select_for_update()
            .filter(pk=offer_id, conversation=conversation)
            .first()
        )
        if (
            not offer
            or offer.created_by_id == user.pk
            or offer.status not in ("PROPOSED", "REVISION_REQUESTED")
            or offer.expires_at <= timezone.now()
        ):
            raise ValidationError("This quote cannot be revised.")
        offer.status = "REVISION_REQUESTED"
        offer.save(update_fields=["status", "updated_at"])
        record_conversation_event(
            conversation=conversation,
            event_code="QUOTE_REVISION_REQUESTED",
            event_key=f"offer:{offer.pk}:revision-requested",
            body="The customer requested a revised formal quote. No new amount has been accepted.",
        )
        return offer


def quote_payload_hash(payload):
    import hashlib, json
    from django.core.serializers.json import DjangoJSONEncoder

    data = {
        k: str(Decimal(str(payload.get(k, 0))).quantize(Decimal("0.01")))
        for k in (
            "labour_amount",
            "materials_amount",
            "transport_amount",
            "additional_amount",
            "total_amount",
        )
    }
    for key in (
        "scope",
        "notes",
        "materials_details",
        "additional_charge_details",
        "payment_terms",
        "exclusions",
        "workmanship_terms",
    ):
        data[key] = str(payload.get(key, "")).strip()
    for key in ("scheduled_start", "scheduled_end", "expires_at"):
        data[key] = payload.get(key)
    data["materials_included"] = bool(payload.get("materials_included", False))
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, cls=DjangoJSONEncoder).encode()
    ).hexdigest()
