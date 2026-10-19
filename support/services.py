from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from notifications.events import (
    notify_support,
    notify_support_case_assigned,
    notify_support_case_closed,
    notify_support_case_created,
    notify_support_case_resolved,
    notify_support_refund_failed,
    notify_support_refund_requested,
    notify_support_refund_succeeded,
    schedule_email,
)
from notifications.links import support_case_link
from booking.models import Booking, BookingStatusHistory
from payments.flutterwave import (
    FlutterwaveClient,
    FlutterwaveError,
    FlutterwaveUnknownError,
)
from payments.models import EscrowPayment
from promotions.models import PromotionPayment

from .models import (
    SupportCase,
    SupportCaseEvent,
    SupportFinancialAction,
)
from .state import require_transition
from core.state import BOOKING_TRANSITIONS, PAYMENT_TRANSITIONS, require_transition as require_transaction_transition
from core.services import create_audit_log

from .validation import normalize_support_description
from .permissions import (
    SUPPORT_ADMIN,
    SUPPORT_AGENT,
    SUPPORT_FINANCE,
    SUPPORT_SUPERVISOR,
    user_has_support_role,
)


FINANCIAL_RESOLUTIONS = {
    SupportCase.ResolutionCode.REFUND,
    SupportCase.ResolutionCode.PAYOUT,
    SupportCase.ResolutionCode.PARTIAL_REFUND,
    SupportCase.ResolutionCode.PARTIAL_PAYOUT,
}

REFUND_REQUEST_ROLES = {
    SUPPORT_AGENT,
    SUPPORT_SUPERVISOR,
    SUPPORT_ADMIN,
}

REFUND_APPROVAL_ROLES = {
    SUPPORT_SUPERVISOR,
    SUPPORT_ADMIN,
}

REFUND_EXECUTION_ROLES = {
    SUPPORT_FINANCE,
    SUPPORT_ADMIN,
}



def _conversation_case_event(case, event_code, action_suffix, metadata=None):
    metadata = metadata or {}
    if case.conversation_id:
        from conversations.events import record_conversation_event
        from conversations.models import Conversation
        transaction.on_commit(
            lambda case_id=case.id, conversation_id=case.conversation_id, event_code=event_code, action_suffix=action_suffix, metadata=metadata: record_conversation_event(
                conversation=Conversation.objects.get(pk=conversation_id),
                event_code=event_code,
                event_key=f"support:{case_id}:{action_suffix}",
                metadata=metadata,
            )
        )
        return
    if not case.booking_id:
        return
    from booking.models import Booking
    from conversations.events import record_booking_event
    transaction.on_commit(
        lambda case_id=case.id, booking_id=case.booking_id, event_code=event_code, action_suffix=action_suffix, metadata=metadata: record_booking_event(
            booking=Booking.objects.select_related("request", "customer__user").get(pk=booking_id),
            event_code=event_code,
            event_key=f"support:{case_id}:{action_suffix}",
            metadata=metadata,
        )
    )


def _money_to_kobo(value):
    try:
        amount = Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("Refund amount must be a valid decimal amount.") from exc
    if not amount.is_finite():
        raise ValueError("Refund amount must be finite.")
    kobo = int(
        (amount * 100).to_integral_value(rounding=ROUND_HALF_UP)
    )

    if kobo <= 0:
        raise ValueError("Refund amount must be greater than zero.")

    return kobo


def _refundable_amount_kobo(payment):
    active_statuses = [
        SupportFinancialAction.Status.REQUESTED,
        SupportFinancialAction.Status.APPROVED,
        SupportFinancialAction.Status.PROCESSING,
        SupportFinancialAction.Status.PROCESSING_UNKNOWN,
        SupportFinancialAction.Status.SUCCEEDED,
    ]
    is_promotion = isinstance(payment, PromotionPayment)
    payment_filter = {"promotion_payment" if is_promotion else "payment": payment}
    reserved = (
        SupportFinancialAction.objects
        .filter(**payment_filter, status__in=active_statuses)
        .exclude(status=SupportFinancialAction.Status.FAILED)
        .values_list("amount_kobo", flat=True)
    )
    reserved_kobo = sum(reserved)
    original_kobo = payment.amount_kobo if is_promotion else payment.total_amount_kobo
    return max(original_kobo - reserved_kobo, 0)


def _support_action_payment(action, *, lock=False):
    if action.payment_id:
        queryset = EscrowPayment.objects
        if lock:
            queryset = queryset.select_for_update(of=("self",))
        return queryset.get(pk=action.payment_id)
    if action.promotion_payment_id:
        queryset = PromotionPayment.objects
        if lock:
            queryset = queryset.select_for_update(of=("self",))
        return queryset.get(pk=action.promotion_payment_id)
    raise ValueError("The refund record is not linked to a supported payment.")


def _payment_amount_kobo(payment):
    return payment.amount_kobo if isinstance(payment, PromotionPayment) else payment.total_amount_kobo


def _payment_transaction_id(payment):
    return payment.provider_transaction_id


def _payment_eligible_status(payment):
    if isinstance(payment, PromotionPayment):
        return payment.status in {
            PromotionPayment.Status.VERIFIED,
            PromotionPayment.Status.PARTIALLY_REFUNDED,
            PromotionPayment.Status.REVIEW_REQUIRED,
        }
    return payment.status in {EscrowPayment.Status.HELD, EscrowPayment.Status.DISPUTED}


def _payment_refund_pending(payment):
    if isinstance(payment, PromotionPayment):
        payment.status = PromotionPayment.Status.REFUND_PENDING
    else:
        payment.status = EscrowPayment.Status.REFUND_PENDING


def _payment_status_before_refund(payment):
    return payment.status


def _payment_status_after_refund(payment, refunded_kobo):
    if isinstance(payment, PromotionPayment):
        payment.status = (
            PromotionPayment.Status.REFUNDED
            if refunded_kobo >= payment.amount_kobo
            else PromotionPayment.Status.PARTIALLY_REFUNDED
        )
    else:
        payment.status = (
            EscrowPayment.Status.REFUNDED
            if refunded_kobo >= payment.total_amount_kobo
            else EscrowPayment.Status.HELD
        )


def _payment_status_after_failed_refund(payment, status_before):
    if isinstance(payment, PromotionPayment):
        payment.status = status_before if status_before in {
            PromotionPayment.Status.VERIFIED,
            PromotionPayment.Status.PARTIALLY_REFUNDED,
            PromotionPayment.Status.REVIEW_REQUIRED,
        } else PromotionPayment.Status.VERIFIED
    else:
        payment.status = EscrowPayment.Status.HELD



@transaction.atomic
def create_support_case(
    *,
    opened_by,
    case_type,
    subject,
    description,
    booking=None,
    conversation=None,
    promotion_campaign=None,
    priority="MEDIUM",
    booking_status_before_dispute=None,
):
    description = normalize_support_description(description)
    if (booking is not None and promotion_campaign is not None) or (conversation is not None and promotion_campaign is not None):
        raise ValueError("A support case can reference a booking or conversation, or a promotion campaign, not both.")
    if conversation is not None:
        is_participant = bool(
            conversation.customer_id == opened_by.id
            or (conversation.artisan_id and conversation.artisan.user_id == opened_by.id)
            or conversation.participants.filter(pk=opened_by.id).exists()
        )
        if not is_participant:
            raise ValueError("The case opener must participate in the linked conversation.")
        if booking is not None and not (
            conversation.conversation_type == "SERVICE"
            and conversation.service_request_id == booking.request_id
            and conversation.customer_id == booking.customer.user_id
            and conversation.artisan_id == booking.artisan_id
            and booking.request.requester_id == booking.customer.user_id
        ):
            raise ValueError("The linked conversation does not match the booking's customer, artisan, and service request.")
    if case_type == SupportCase.CaseType.DISPUTE:
        if booking is None:
            raise ValueError("A dispute case must reference a booking.")
        if booking.customer.user_id != opened_by.id:
            raise ValueError("Only the booking customer can open a dispute case.")
        if booking.request.requester_id != booking.customer.user_id:
            raise ValueError("The booking does not match its service request customer.")
    case = SupportCase.objects.create(
        opened_by=opened_by,
        booking=booking,
        conversation=conversation,
        promotion_campaign=promotion_campaign,
        case_type=case_type,
        subject=subject,
        description=description,
        priority=priority,
    )

    SupportCaseEvent.objects.create(
        case=case,
        action="CASE_CREATED",
        performed_by=opened_by,
        note=description,
        metadata=(
            {"booking_status_before_dispute": str(booking_status_before_dispute)}
            if booking_status_before_dispute is not None
            else {}
        ),
    )

    _conversation_case_event(case, "SUPPORT_CASE_CREATED", "created", {"case_id": case.id})
    notify_support_case_created(case)
    create_audit_log(action="SUPPORT_CASE_CREATED", resource_type="SupportCase", resource_id=str(case.id), actor=opened_by, metadata={"case_type": case.case_type, "booking_id": str(case.booking_id) if case.booking_id else None, "conversation_id": str(case.conversation_id) if case.conversation_id else None, "promotion_campaign_id": str(case.promotion_campaign_id) if case.promotion_campaign_id else None})

    return case


@transaction.atomic
def assign_support_case(*, case, staff_user):
    if not user_has_support_role(
        staff_user,
        {SUPPORT_SUPERVISOR, SUPPORT_ADMIN},
    ):
        raise ValueError(
            "Customer-care supervisor access is required."
        )

    if case.status in {SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED}: raise ValueError("A resolved or closed case cannot be assigned.")
    if case.status != SupportCase.Status.UNDER_REVIEW: require_transition(case.status, SupportCase.Status.UNDER_REVIEW)

    case.assigned_to = staff_user

    if case.status == SupportCase.Status.OPEN:
        case.status = SupportCase.Status.UNDER_REVIEW

    case.save(
        update_fields=["assigned_to", "status", "updated_at"]
    )

    SupportCaseEvent.objects.create(
        case=case,
        action="CASE_ASSIGNED",
        performed_by=staff_user,
        note="Case assigned to customer care.",
    )

    _conversation_case_event(case, "SUPPORT_CASE_UPDATED", f"event-{case.id}-assigned", {"case_id": case.id})
    notify_support_case_assigned(case)
    create_audit_log(action="SUPPORT_CASE_ASSIGNED", resource_type="SupportCase", resource_id=str(case.id), actor=staff_user, metadata={"assigned_to": staff_user.id})

    return case


@transaction.atomic
def claim_support_case(*, case, staff_user):
    if not user_has_support_role(
        staff_user,
        {SUPPORT_AGENT, SUPPORT_SUPERVISOR, SUPPORT_ADMIN},
    ):
        raise ValueError("Customer-care access is required.")

    if case.status in {SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED}: raise ValueError("A resolved or closed case cannot be claimed.")

    if (
        case.assigned_to_id
        and case.assigned_to_id != staff_user.id
    ):
        raise ValueError(
            "This case is already assigned to another support user."
        )

    case.assigned_to = staff_user
    case.status = SupportCase.Status.UNDER_REVIEW

    case.save(
        update_fields=["assigned_to", "status", "updated_at"]
    )

    SupportCaseEvent.objects.create(
        case=case,
        action="CASE_CLAIMED",
        performed_by=staff_user,
        note="Case claimed by customer care.",
    )

    notify_support_case_assigned(case)
    create_audit_log(action="SUPPORT_CASE_CLAIMED", resource_type="SupportCase", resource_id=str(case.id), actor=staff_user)

    return case


@transaction.atomic
def add_case_note(*, case, staff_user, note):
    if not user_has_support_role(staff_user):
        raise ValueError("Customer-care access is required.")

    if case.status == SupportCase.Status.CLOSED:
        raise ValueError(
            "Closed cases cannot receive new support notes."
        )

    case_event = SupportCaseEvent.objects.create(
        case=case,
        action="CASE_NOTE_ADDED",
        performed_by=staff_user,
        note=note,
    )
    _conversation_case_event(case, "SUPPORT_CASE_UPDATED", f"event-{case_event.id}", {"case_id": case.id, "support_event_id": case_event.id})
    create_audit_log(action="SUPPORT_CASE_NOTE_ADDED", resource_type="SupportCase", resource_id=str(case.id), actor=staff_user, metadata={"support_event_id": case_event.id})

    return case


@transaction.atomic
def send_case_message(*, case, sender, message):
    """Record a public support-thread message and notify the receiving side."""
    message = str(message or "").strip()
    if len(message) < 2 or len(message) > 5000:
        raise ValueError("Message must contain between 2 and 5,000 characters.")
    if case.status in {SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED}:
        raise ValueError("This support case is resolved or closed and cannot receive new messages.")

    sender_is_support = user_has_support_role(sender)
    if sender_is_support:
        sender_role = "SUPPORT"
    elif case.booking_id and case.booking.customer.user_id == sender.id:
        sender_role = "CUSTOMER"
    elif case.booking_id and case.booking.artisan.user_id == sender.id:
        sender_role = "ARTISAN"
    elif case.promotion_campaign_id and case.promotion_campaign.artisan.user_id == sender.id:
        sender_role = "ARTISAN"
    elif case.conversation_id and case.conversation.customer_id == sender.id:
        sender_role = "CUSTOMER"
    elif case.conversation_id and case.conversation.artisan_id and case.conversation.artisan.user_id == sender.id:
        sender_role = "ARTISAN"
    elif case.conversation_id and case.conversation.participants.filter(pk=sender.id).exists():
        sender_role = "CONVERSATION_PARTICIPANT"
    elif case.opened_by_id == sender.id:
        sender_role = "CUSTOMER_OR_ARTISAN"
    else:
        raise PermissionDenied("Only a case participant or customer-support staff can send a message.")

    event = SupportCaseEvent.objects.create(
        case=case,
        action="CASE_MESSAGE_SENT",
        performed_by=sender,
        note=message,
        metadata={"visibility": "PUBLIC", "sender_role": sender_role},
    )
    create_audit_log(
        action="SUPPORT_CASE_MESSAGE_SENT",
        resource_type="SupportCase",
        resource_id=str(case.id),
        actor=sender,
        metadata={"support_event_id": event.id, "sender_role": sender_role},
    )

    if sender_is_support:
        recipients = {case.opened_by_id: case.opened_by}
        if case.booking_id:
            recipients[case.booking.customer.user_id] = case.booking.customer.user
            recipients[case.booking.artisan.user_id] = case.booking.artisan.user
        if case.promotion_campaign_id:
            recipients[case.promotion_campaign.artisan.user_id] = case.promotion_campaign.artisan.user
        for recipient in recipients.values():
            if recipient and recipient.id != sender.id:
                schedule_email(
                    event_key=f"support-case:{case.id}:message:{event.id}:recipient:{recipient.id}",
                    recipient=recipient,
                    subject=f"Customer support replied to case {case.reference}",
                    message=f"Customer support added a reply to {case.reference}: {message}",
                    action_url=support_case_link(case),
                )
    else:
        notify_support(
            event_key=f"support-case:{case.id}:message:{event.id}",
            subject=f"New message on support case {case.reference}",
            message=f"{sender.get_full_name() or sender.email} sent a message on {case.reference} ({case.subject}): {message}",
        )
        if case.assigned_to_id:
            schedule_email(
                event_key=f"support-case:{case.id}:message:{event.id}:assigned:{case.assigned_to_id}",
                recipient=case.assigned_to,
                subject=f"New message on your case {case.reference}",
                message=f"A case participant sent a new message: {message}",
                action_url=support_case_link(case),
            )

    return event


def _previous_status_before_dispute(*, case, booking):
    """Recover the real pre-dispute state without guessing from current frontend input."""
    created_event = (
        SupportCaseEvent.objects.filter(case=case, action="CASE_CREATED")
        .only("metadata")
        .order_by("created_at", "pk")
        .first()
    )
    if created_event and isinstance(created_event.metadata, dict):
        recorded_status = str(created_event.metadata.get("booking_status_before_dispute") or "")
        if recorded_status in {
            Booking.Status.ACCEPTED,
            Booking.Status.IN_PROGRESS,
            Booking.Status.COMPLETION_REQUESTED,
        }:
            return recorded_status

    dispute_history = (
        BookingStatusHistory.objects.filter(booking=booking, status=Booking.Status.DISPUTED)
        .order_by("-created_at", "-pk")
        .first()
    )
    history = BookingStatusHistory.objects.filter(booking=booking)
    if dispute_history:
        history = history.filter(created_at__lte=dispute_history.created_at)
    previous_event = history.exclude(status=Booking.Status.DISPUTED).order_by("-created_at", "-pk").first()
    previous_status = previous_event.status if previous_event else ""
    if previous_status not in {
        Booking.Status.ACCEPTED,
        Booking.Status.IN_PROGRESS,
        Booking.Status.COMPLETION_REQUESTED,
    }:
        raise ValueError(
            "The booking's state before the dispute could not be verified. Keep the case open for a support supervisor to review the booking history."
        )
    return previous_status


def _apply_dispute_transaction_resolution(*, case, booking, payments, resolution_code, staff_user, resolved_at):
    """Unfreeze or cancel only the linked booking after its real financial outcome is known."""
    if not booking or booking.status != Booking.Status.DISPUTED:
        raise ValueError("The linked booking is no longer in a disputed state; review its transaction before resolving this case.")

    if resolution_code == SupportCase.ResolutionCode.REFUND:
        if not payments or not any(payment.status == EscrowPayment.Status.REFUNDED for payment in payments):
            raise ValueError("A full-refund resolution requires a provider-verified completed refund.")
        if any(payment.status not in {EscrowPayment.Status.REFUNDED, EscrowPayment.Status.FAILED} for payment in payments):
            raise ValueError("Every booking payment must be fully refunded or failed before the dispute can be resolved as a full refund.")
        target_status = Booking.Status.CANCELLED
    else:
        target_status = _previous_status_before_dispute(case=case, booking=booking)
        if any(payment.status in {EscrowPayment.Status.REFUND_PENDING, EscrowPayment.Status.RELEASE_PENDING} for payment in payments):
            raise ValueError("Wait for the pending booking payment transaction to finish before resolving the dispute.")
        for payment in payments:
            if payment.status != EscrowPayment.Status.DISPUTED:
                continue
            require_transaction_transition(
                PAYMENT_TRANSITIONS,
                payment.status,
                EscrowPayment.Status.HELD,
                resource="payment",
            )
            payment.status = EscrowPayment.Status.HELD
            payment.failure_reason = ""
            payment.save(update_fields=["status", "failure_reason", "updated_at"])

    require_transaction_transition(
        BOOKING_TRANSITIONS,
        booking.status,
        target_status,
        resource="booking",
    )
    booking.status = target_status
    booking.finalized_at = resolved_at if target_status == Booking.Status.CANCELLED else None
    booking.save(update_fields=["status", "finalized_at", "updated_at"])
    BookingStatusHistory.objects.create(
        booking=booking,
        status=target_status,
        changed_by="ADMIN",
        changed_by_user=staff_user,
        notes=f"Support case {case.reference} resolved with outcome {resolution_code}.",
    )
    return target_status


@transaction.atomic
def resolve_support_case(
    *,
    case,
    staff_user,
    resolution_code,
    resolution_note,
):
    if not user_has_support_role(
        staff_user,
        {SUPPORT_SUPERVISOR, SUPPORT_ADMIN},
    ):
        raise ValueError("Customer-care supervisor access is required.")

    case = SupportCase.objects.select_for_update(of=("self",)).get(pk=case.pk)
    if case.status in {SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED}:
        raise ValueError("This case has already been resolved or closed.")

    if resolution_code in {SupportCase.ResolutionCode.PAYOUT, SupportCase.ResolutionCode.PARTIAL_PAYOUT}:
        raise ValueError("Payout outcomes must use a verified payment-release workflow and cannot be recorded as a case-only resolution.")

    if resolution_code in {SupportCase.ResolutionCode.REFUND, SupportCase.ResolutionCode.PARTIAL_REFUND}:
        verified_refund_exists = SupportFinancialAction.objects.filter(
            case=case,
            status=SupportFinancialAction.Status.SUCCEEDED,
            action_type__in={
                SupportFinancialAction.ActionType.REFUND,
                SupportFinancialAction.ActionType.PARTIAL_REFUND,
            },
        ).exists()
        if case.resolution_code != resolution_code or not verified_refund_exists:
            raise ValueError("Refund resolutions can only be recorded after the matching provider-verified refund workflow has succeeded.")
    elif case.resolution_code in FINANCIAL_RESOLUTIONS:
        raise ValueError(
            f"This case already has a verified {case.resolution_code.lower().replace('_', ' ')} outcome. Select that recorded financial outcome to preserve the transaction record."
        )

    pending_financial_actions = SupportFinancialAction.objects.filter(
        case=case,
        status__in={
            SupportFinancialAction.Status.REQUESTED,
            SupportFinancialAction.Status.APPROVED,
            SupportFinancialAction.Status.PROCESSING,
            SupportFinancialAction.Status.PROCESSING_UNKNOWN,
        },
    ).exists()
    if pending_financial_actions:
        raise ValueError("This case has a pending financial action and cannot be resolved yet.")

    booking = None
    payments = []
    if case.case_type == SupportCase.CaseType.DISPUTE:
        if not case.booking_id:
            raise ValueError("This dispute has no linked booking transaction and cannot be resolved safely.")
        booking = Booking.objects.select_for_update(of=("self",)).get(pk=case.booking_id)
        payments = list(
            EscrowPayment.objects.select_for_update(of=("self",))
            .filter(booking=booking)
            .order_by("pk")
        )
        if resolution_code == SupportCase.ResolutionCode.REFUND and any(
            payment.status not in {EscrowPayment.Status.REFUNDED, EscrowPayment.Status.FAILED}
            for payment in payments
        ):
            raise ValueError("All linked booking payments must have a verified terminal refund or failure state before this dispute can be resolved as a full refund.")

    require_transition(case.status, SupportCase.Status.RESOLVED)
    resolved_at = timezone.now()
    booking_status_after = None
    if booking:
        booking_status_after = _apply_dispute_transaction_resolution(
            case=case,
            booking=booking,
            payments=payments,
            resolution_code=resolution_code,
            staff_user=staff_user,
            resolved_at=resolved_at,
        )

    case.status = SupportCase.Status.RESOLVED
    case.resolution_code = resolution_code
    case.resolution_note = resolution_note
    case.resolved_at = resolved_at
    if case.assigned_to_id is None:
        case.assigned_to = staff_user
    case.save(
        update_fields=[
            "status",
            "resolution_code",
            "resolution_note",
            "resolved_at",
            "assigned_to",
            "updated_at",
        ]
    )

    event_metadata = {"resolution_code": resolution_code}
    conversation_metadata = {"case_id": case.id, "resolution_code": resolution_code}
    if booking_status_after:
        event_metadata["booking_status"] = booking_status_after
        conversation_metadata["booking_status"] = booking_status_after
    SupportCaseEvent.objects.create(
        case=case,
        action="CASE_RESOLVED",
        performed_by=staff_user,
        note=resolution_note,
        metadata=event_metadata,
    )

    _conversation_case_event(case, "SUPPORT_CASE_RESOLVED", "resolved", conversation_metadata)
    notify_support_case_resolved(case)
    create_audit_log(
        action="SUPPORT_CASE_RESOLVED",
        resource_type="SupportCase",
        resource_id=str(case.id),
        actor=staff_user,
        metadata={
            "resolution_code": resolution_code,
            "booking_id": str(case.booking_id) if case.booking_id else None,
            "booking_status": booking_status_after,
        },
    )

    return case

@transaction.atomic
def close_support_case(*, case, staff_user):
    if not user_has_support_role(
        staff_user,
        {SUPPORT_SUPERVISOR, SUPPORT_ADMIN},
    ):
        raise ValueError("Customer-care supervisor access is required.")

    case = SupportCase.objects.select_for_update(of=("self",)).get(pk=case.pk)
    if case.status != SupportCase.Status.RESOLVED:
        raise ValueError("Only a resolved case can be closed.")

    if case.case_type == SupportCase.CaseType.DISPUTE:
        if not case.booking_id:
            raise ValueError("This dispute has no linked booking transaction and cannot be closed safely.")
        booking = Booking.objects.select_for_update(of=("self",)).get(pk=case.booking_id)
        if booking.status == Booking.Status.DISPUTED:
            raise ValueError("Resolve the linked booking transaction before closing this dispute case.")
        payments = list(
            EscrowPayment.objects.select_for_update(of=("self",))
            .filter(booking=booking)
            .order_by("pk")
        )
        unsettled_statuses = {
            EscrowPayment.Status.PAYMENT_PENDING,
            EscrowPayment.Status.HELD,
            EscrowPayment.Status.DISPUTED,
            EscrowPayment.Status.RELEASE_PENDING,
            EscrowPayment.Status.REFUND_PENDING,
        }
        if any(payment.status in unsettled_statuses for payment in payments):
            raise ValueError("Dispute payments must be settled through a verified payment workflow before the case can be closed.")

    require_transition(case.status, SupportCase.Status.CLOSED)
    case.status = SupportCase.Status.CLOSED
    case.closed_at = timezone.now()
    case.save(update_fields=["status", "closed_at", "updated_at"])

    SupportCaseEvent.objects.create(
        case=case,
        action="CASE_CLOSED",
        performed_by=staff_user,
        note="Case closed.",
    )

    _conversation_case_event(case, "SUPPORT_CASE_CLOSED", "closed", {"case_id": case.id})
    notify_support_case_closed(case)
    create_audit_log(action="SUPPORT_CASE_CLOSED", resource_type="SupportCase", resource_id=str(case.id), actor=staff_user)
    return case

@transaction.atomic
def create_refund_request(
    *,
    case,
    staff_user,
    payment=None,
    promotion_payment=None,
    amount,
    reason,
    idempotency_key,
):
    if not user_has_support_role(staff_user, REFUND_REQUEST_ROLES):
        raise ValueError("Customer-care refund request access is required.")
    if (payment is None) == (promotion_payment is None):
        raise ValueError("Select exactly one booking escrow payment or promotion payment.")

    case = SupportCase.objects.select_for_update(of=("self",)).get(pk=case.pk)
    if case.status not in {SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW}:
        raise ValueError("Refunds can only be requested while the support case is open or under review.")
    if case.case_type not in {SupportCase.CaseType.PAYMENT, SupportCase.CaseType.DISPUTE}:
        raise ValueError("Refunds can only be requested for payment or dispute cases.")

    if promotion_payment is not None:
        promotion_payment = PromotionPayment.objects.select_for_update(of=("self",)).get(pk=promotion_payment.pk)
        if not case.promotion_campaign_id or promotion_payment.campaign_id != case.promotion_campaign_id:
            raise ValueError("The promotion payment does not belong to the support case campaign.")
        if case.booking_id:
            raise ValueError("A promotion refund case cannot also be attached to a booking.")
        if promotion_payment.status not in {
            PromotionPayment.Status.VERIFIED,
            PromotionPayment.Status.PARTIALLY_REFUNDED,
            PromotionPayment.Status.REVIEW_REQUIRED,
        }:
            raise ValueError("Only verified promotion payments can enter the refund workflow.")
        if not promotion_payment.provider_transaction_id:
            raise ValueError("The promotion payment has no verified Flutterwave transaction ID.")
        payment_object = promotion_payment
        payment_field = {"promotion_payment": promotion_payment}
    else:
        payment = EscrowPayment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
        if case.booking_id != payment.booking_id or case.promotion_campaign_id:
            raise ValueError("The payment does not belong to the support case booking.")
        if payment.status not in {EscrowPayment.Status.HELD, EscrowPayment.Status.DISPUTED}:
            raise ValueError("Only held or disputed payments can enter the refund workflow.")
        if not payment.provider_transaction_id:
            raise ValueError("The payment has no verified Flutterwave transaction ID.")
        payment_object = payment
        payment_field = {"payment": payment}

    existing = SupportFinancialAction.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.requested_by_id != staff_user.id:
            raise ValueError("This idempotency key is already in use.")
        return existing

    amount_kobo = _money_to_kobo(amount)
    if amount_kobo < 10_000:
        raise ValueError("Flutterwave refunds must be at least NGN 100.")
    if amount_kobo % 100:
        raise ValueError("Refund amounts must be in whole NGN because the current Flutterwave v3 integration accepts integer NGN amounts.")
    refundable_kobo = _refundable_amount_kobo(payment_object)
    if amount_kobo > refundable_kobo:
        raise ValueError("Refund amount exceeds the remaining refundable payment amount.")
    original_kobo = _payment_amount_kobo(payment_object)
    action_type = SupportFinancialAction.ActionType.REFUND if amount_kobo == original_kobo else SupportFinancialAction.ActionType.PARTIAL_REFUND

    try:
        with transaction.atomic():
            action = SupportFinancialAction.objects.create(
                case=case,
                **payment_field,
                action_type=action_type,
                amount_kobo=amount_kobo,
                reason=reason,
                idempotency_key=idempotency_key,
                requested_by=staff_user,
            )
    except IntegrityError:
        existing = SupportFinancialAction.objects.filter(idempotency_key=idempotency_key).first()
        if existing is None or existing.requested_by_id != staff_user.id:
            raise ValueError("This idempotency key is already in use.") from None
        return existing

    SupportCaseEvent.objects.create(
        case=case,
        action="REFUND_REQUESTED",
        performed_by=staff_user,
        note=reason,
        metadata={
            "refund_reference": action.reference,
            "amount_kobo": amount_kobo,
            "promotion_payment_id": str(promotion_payment.id) if promotion_payment else None,
        },
    )
    _conversation_case_event(action.case, "REFUND_PENDING", f"refund-{action.id}-requested", {"case_id": action.case_id, "refund_reference": action.reference, "amount_kobo": amount_kobo})
    notify_support_refund_requested(action)
    create_audit_log(action="REFUND_REQUESTED", resource_type="SupportFinancialAction", resource_id=str(action.id), actor=staff_user, metadata={"case_id": str(action.case_id), "payment_id": str(action.payment_id) if action.payment_id else None, "promotion_payment_id": str(action.promotion_payment_id) if action.promotion_payment_id else None, "amount_kobo": amount_kobo})
    return action

@transaction.atomic
def approve_refund(*, action, staff_user, approval_note):
    if not user_has_support_role(
        staff_user,
        REFUND_APPROVAL_ROLES,
    ):
        raise ValueError(
            "Customer-care supervisor access is required to approve refunds."
        )

    action = SupportFinancialAction.objects.select_for_update(of=("self",)).get(
        pk=action.pk
    )

    if action.status != SupportFinancialAction.Status.REQUESTED:
        raise ValueError("Only requested refunds can be approved.")

    if (
        action.requested_by_id == staff_user.id
        and not staff_user.is_superuser
    ):
        raise ValueError(
            "Refund approval must be performed by a different support user."
        )

    action.status = SupportFinancialAction.Status.APPROVED
    action.approved_by = staff_user
    action.approved_at = timezone.now()

    action.save(
        update_fields=[
            "status", "approved_by", "approved_at", "updated_at"
        ]
    )

    SupportCaseEvent.objects.create(
        case=action.case,
        action="REFUND_APPROVED",
        performed_by=staff_user,
        note=approval_note,
        metadata={"refund_reference": action.reference},
    )

    create_audit_log(action="REFUND_APPROVED", resource_type="SupportFinancialAction", resource_id=str(action.id), actor=staff_user, metadata={"case_id": str(action.case_id)})
    return action


@transaction.atomic
def reject_refund(*, action, staff_user, rejection_note):
    if not user_has_support_role(
        staff_user,
        REFUND_APPROVAL_ROLES,
    ):
        raise ValueError(
            "Customer-care supervisor access is required to reject refunds."
        )

    action = SupportFinancialAction.objects.select_for_update(of=("self",)).get(
        pk=action.pk
    )

    if action.status != SupportFinancialAction.Status.REQUESTED:
        raise ValueError("Only requested refunds can be rejected.")

    if (
        action.requested_by_id == staff_user.id
        and not staff_user.is_superuser
    ):
        raise ValueError(
            "Refund rejection must be performed by a different support user."
        )

    action.status = SupportFinancialAction.Status.REJECTED
    action.approved_by = staff_user
    action.approved_at = timezone.now()
    action.failure_reason = rejection_note

    action.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "failure_reason",
            "updated_at",
        ]
    )

    SupportCaseEvent.objects.create(
        case=action.case,
        action="REFUND_REJECTED",
        performed_by=staff_user,
        note=rejection_note,
        metadata={"refund_reference": action.reference},
    )

    create_audit_log(action="REFUND_REJECTED", resource_type="SupportFinancialAction", resource_id=str(action.id), actor=staff_user, metadata={"case_id": str(action.case_id)})
    return action


def execute_refund(*, action, staff_user):
    if not user_has_support_role(staff_user, REFUND_EXECUTION_ROLES):
        raise ValueError("Customer-care finance access is required to execute refunds.")

    with transaction.atomic():
        action = (
            SupportFinancialAction.objects.select_for_update(of=("self",))
            .select_related("payment", "promotion_payment", "case")
            .get(pk=action.pk)
        )
        if action.status == SupportFinancialAction.Status.SUCCEEDED:
            return action
        if action.status == SupportFinancialAction.Status.PROCESSING:
            raise ValueError("This refund is already processing.")
        if action.status == SupportFinancialAction.Status.PROCESSING_UNKNOWN:
            raise ValueError("This refund has an unknown provider outcome and must be reconciled before another submission.")
        if action.status != SupportFinancialAction.Status.APPROVED:
            raise ValueError("Only approved refunds can be executed.")
        if not action.approved_by_id:
            raise ValueError("The refund approval record is missing.")
        if action.approved_by_id == staff_user.id and not staff_user.is_superuser:
            raise ValueError("Refund execution must be performed by a different support user.")

        payment = _support_action_payment(action, lock=True)
        if not _payment_eligible_status(payment):
            raise ValueError("The payment is no longer eligible for this refund.")
        transaction_id = _payment_transaction_id(payment)
        if not transaction_id:
            raise ValueError("The payment has no verified Flutterwave transaction ID.")
        if not action.payment_status_before:
            action.payment_status_before = payment.status
        action.status = SupportFinancialAction.Status.PROCESSING
        action.executed_by = staff_user
        action.submitted_at = timezone.now()
        action.save(update_fields=["status", "executed_by", "submitted_at", "payment_status_before", "updated_at"])
        _payment_refund_pending(payment)
        update_fields = ["status", "updated_at"]
        if isinstance(payment, EscrowPayment):
            payment.failure_reason = ""
            update_fields.append("failure_reason")
        payment.save(update_fields=update_fields)
        create_audit_log(action="REFUND_EXECUTION_STARTED", resource_type="SupportFinancialAction", resource_id=str(action.id), actor=staff_user, metadata={"case_id": str(action.case_id), "payment_id": str(action.payment_id) if action.payment_id else None, "promotion_payment_id": str(action.promotion_payment_id) if action.promotion_payment_id else None, "amount_kobo": action.amount_kobo})

    try:
        result = FlutterwaveClient().refund_transaction(
            transaction_id=transaction_id,
            amount_kobo=action.amount_kobo,
            comments=action.reason,
            callback_url=getattr(settings, "FLW_REFUND_CALLBACK_URL", ""),
        )
    except FlutterwaveUnknownError as exc:
        with transaction.atomic():
            locked = SupportFinancialAction.objects.select_for_update(of=("self",)).get(pk=action.pk)
            locked.status = SupportFinancialAction.Status.PROCESSING_UNKNOWN
            locked.failure_reason = str(exc)
            locked.save(update_fields=["status", "failure_reason", "updated_at"])
            SupportCaseEvent.objects.create(case_id=locked.case_id, action="REFUND_SUBMISSION_UNKNOWN", performed_by=staff_user, note="Flutterwave did not return a definitive refund submission result.", metadata={"refund_reference": locked.reference})
            case = SupportCase.objects.get(pk=locked.case_id)
            _conversation_case_event(case, "REFUND_PENDING", f"refund-{locked.id}-unknown", {"refund_reference": locked.reference})
            create_audit_log(action="REFUND_EXECUTION_UNKNOWN", resource_type="SupportFinancialAction", resource_id=str(locked.id), actor=staff_user, metadata={"case_id": str(locked.case_id), "payment_id": str(locked.payment_id) if locked.payment_id else None, "promotion_payment_id": str(locked.promotion_payment_id) if locked.promotion_payment_id else None, "refund_reference": locked.reference})
        return locked
    except FlutterwaveError as exc:
        with transaction.atomic():
            locked = SupportFinancialAction.objects.select_for_update(of=("self",)).get(pk=action.pk)
            locked.status = SupportFinancialAction.Status.FAILED
            locked.failure_reason = str(exc)
            locked.save(update_fields=["status", "failure_reason", "updated_at"])
            payment = _support_action_payment(locked, lock=True)
            _payment_status_after_failed_refund(payment, locked.payment_status_before)
            if isinstance(payment, EscrowPayment):
                payment.failure_reason = str(exc)
                payment.save(update_fields=["status", "failure_reason", "updated_at"])
            else:
                payment.save(update_fields=["status", "updated_at"])
            SupportCaseEvent.objects.create(case_id=locked.case_id, action="REFUND_FAILED", performed_by=staff_user, note=str(exc), metadata={"refund_reference": locked.reference})
            notify_support_refund_failed(locked)
            case = SupportCase.objects.get(pk=locked.case_id)
            _conversation_case_event(case, "REFUND_FAILED", f"refund-{locked.id}-failed", {"refund_reference": locked.reference})
            create_audit_log(action="REFUND_EXECUTION_FAILED", resource_type="SupportFinancialAction", resource_id=str(locked.id), actor=staff_user, metadata={"case_id": str(locked.case_id), "payment_id": str(locked.payment_id) if locked.payment_id else None, "promotion_payment_id": str(locked.promotion_payment_id) if locked.promotion_payment_id else None, "refund_reference": locked.reference})
        return locked

    data = result.get("data") or {}
    provider_refund_id = data.get("id")
    provider_ref = data.get("flw_ref") or data.get("reference") or ""
    provider_status = str(data.get("status") or "").lower()
    with transaction.atomic():
        locked = SupportFinancialAction.objects.select_for_update(of=("self",)).get(pk=action.pk)
        locked.provider_refund_id = str(provider_refund_id) if provider_refund_id is not None else None
        locked.provider_ref = provider_ref
        locked.provider_status = provider_status
        locked.provider_payload = result
        locked.status = SupportFinancialAction.Status.PROCESSING
        locked.save(update_fields=["provider_refund_id", "provider_ref", "provider_status", "provider_payload", "status", "updated_at"])
        from .tasks import reconcile_support_refund
        transaction.on_commit(lambda refund_id=str(locked.id): reconcile_support_refund.delay(refund_id))
        SupportCaseEvent.objects.create(case_id=locked.case_id, action="REFUND_SUBMITTED", performed_by=staff_user, note=locked.reason, metadata={"refund_reference": locked.reference, "provider_refund_id": locked.provider_refund_id})
        case = SupportCase.objects.get(pk=locked.case_id)
        _conversation_case_event(case, "REFUND_PENDING", f"refund-{locked.id}-submitted", {"refund_reference": locked.reference, "provider_refund_id": locked.provider_refund_id})
        create_audit_log(action="REFUND_SUBMITTED", resource_type="SupportFinancialAction", resource_id=str(locked.id), actor=staff_user, metadata={"case_id": str(locked.case_id), "payment_id": str(locked.payment_id) if locked.payment_id else None, "promotion_payment_id": str(locked.promotion_payment_id) if locked.promotion_payment_id else None, "provider_refund_id": locked.provider_refund_id})
        return locked

def reconcile_refund(*, action):
    with transaction.atomic():
        action = (
            SupportFinancialAction.objects.select_for_update(of=("self",))
            .select_related("payment", "promotion_payment", "case")
            .get(pk=action.pk)
        )
        if action.status == SupportFinancialAction.Status.SUCCEEDED:
            return action
        if action.status not in {SupportFinancialAction.Status.PROCESSING, SupportFinancialAction.Status.PROCESSING_UNKNOWN}:
            raise ValueError("Only processing refunds can be reconciled.")
        payment = _support_action_payment(action)
        transaction_id = payment.provider_transaction_id
        provider_ref = action.provider_ref
        amount_kobo = action.amount_kobo

    if not transaction_id:
        raise ValueError("The refund's verified source transaction is missing.")
    result = FlutterwaveClient().find_refund(
        transaction_id=transaction_id,
        provider_ref=provider_ref,
        amount_kobo=amount_kobo,
        minimum_date=action.submitted_at or action.requested_at,
    )
    data = result.get("data") or []
    if isinstance(data, dict):
        data = [data]
    match = data[0] if data else None
    if match is None:
        create_audit_log(
            action="REFUND_RECONCILIATION_NO_MATCH",
            resource_type="SupportFinancialAction",
            resource_id=str(action.id),
            actor=action.executed_by or action.approved_by or action.requested_by,
            metadata={"payment_id": str(action.payment_id) if action.payment_id else None, "promotion_payment_id": str(action.promotion_payment_id) if action.promotion_payment_id else None, "refund_reference": action.reference},
        )
        return action

    provider_status = str(match.get("status") or "").lower()
    completed_statuses = {"successful", "succeeded", "completed-bank-transfer", "completed-momo", "completed-mpgs", "completed-offline", "completed-preauth"}
    failed_statuses = {"failed", "cancelled", "canceled"}

    with transaction.atomic():
        locked = (
            SupportFinancialAction.objects.select_for_update(of=("self",))
            .select_related("case")
            .get(pk=action.pk)
        )
        if locked.status == SupportFinancialAction.Status.SUCCEEDED:
            return locked
        locked.provider_refund_id = str(match.get("id")) if match.get("id") is not None else locked.provider_refund_id
        locked.provider_ref = str(match.get("flw_ref") or locked.provider_ref or "")
        locked.provider_status = provider_status
        locked.provider_payload = result
        payment = _support_action_payment(locked, lock=True)
        original_kobo = _payment_amount_kobo(payment)
        payment_filter = {"promotion_payment" if isinstance(payment, PromotionPayment) else "payment": payment}

        if provider_status in completed_statuses:
            locked.status = SupportFinancialAction.Status.SUCCEEDED
            locked.completed_at = timezone.now()
            refunded_total = sum(
                SupportFinancialAction.objects.filter(**payment_filter, status=SupportFinancialAction.Status.SUCCEEDED)
                .exclude(pk=locked.pk)
                .values_list("amount_kobo", flat=True)
            ) + locked.amount_kobo
            _payment_status_after_refund(payment, refunded_total)
            payment_update_fields = ["status", "updated_at"]
            if isinstance(payment, EscrowPayment):
                payment.failure_reason = ""
                payment_update_fields.append("failure_reason")
            payment.save(update_fields=payment_update_fields)
            case = SupportCase.objects.select_for_update(of=("self",)).get(pk=locked.case_id)
            case.resolution_code = SupportCase.ResolutionCode.REFUND if refunded_total >= original_kobo else SupportCase.ResolutionCode.PARTIAL_REFUND
            case.resolution_note = "Promotion payment refund completed through the approved customer-support financial workflow." if isinstance(payment, PromotionPayment) else "Refund completed through the approved customer support financial workflow."
            case.save(update_fields=["resolution_code", "resolution_note", "updated_at"])
            SupportCaseEvent.objects.create(
                case_id=locked.case_id,
                action="REFUND_SUCCEEDED",
                performed_by=locked.executed_by or locked.approved_by or locked.requested_by,
                note="Flutterwave refund completed.",
                metadata={"refund_reference": locked.reference, "provider_refund_id": locked.provider_refund_id, "promotion_payment_id": str(locked.promotion_payment_id) if locked.promotion_payment_id else None},
            )
            _conversation_case_event(case, "REFUNDED" if refunded_total >= original_kobo else "PARTIAL_REFUND_COMPLETED", f"refund-{locked.id}-succeeded", {"refund_reference": locked.reference, "amount_kobo": locked.amount_kobo})
            notify_support_refund_succeeded(locked)
            create_audit_log(action="REFUND_RECONCILED_SUCCEEDED", resource_type="SupportFinancialAction", resource_id=str(locked.id), actor=locked.executed_by or locked.approved_by or locked.requested_by, metadata={"case_id": str(locked.case_id), "payment_id": str(locked.payment_id) if locked.payment_id else None, "promotion_payment_id": str(locked.promotion_payment_id) if locked.promotion_payment_id else None, "amount_kobo": locked.amount_kobo})
        elif provider_status in failed_statuses:
            locked.status = SupportFinancialAction.Status.FAILED
            locked.failure_reason = str(match.get("complete_message") or "Flutterwave refund failed.")
            _payment_status_after_failed_refund(payment, locked.payment_status_before)
            payment_update_fields = ["status", "updated_at"]
            if isinstance(payment, EscrowPayment):
                payment.failure_reason = locked.failure_reason
                payment_update_fields.append("failure_reason")
            payment.save(update_fields=payment_update_fields)
            SupportCaseEvent.objects.create(
                case_id=locked.case_id,
                action="REFUND_FAILED",
                performed_by=locked.executed_by or locked.approved_by or locked.requested_by,
                note=locked.failure_reason,
                metadata={"refund_reference": locked.reference},
            )
            _conversation_case_event(locked.case, "REFUND_FAILED", f"refund-{locked.id}-reconcile-failed", {"refund_reference": locked.reference})
            notify_support_refund_failed(locked)
            create_audit_log(action="REFUND_RECONCILED_FAILED", resource_type="SupportFinancialAction", resource_id=str(locked.id), actor=locked.executed_by or locked.approved_by or locked.requested_by, metadata={"case_id": str(locked.case_id), "payment_id": str(locked.payment_id) if locked.payment_id else None, "promotion_payment_id": str(locked.promotion_payment_id) if locked.promotion_payment_id else None, "refund_reference": locked.reference})
        else:
            locked.status = SupportFinancialAction.Status.PROCESSING

        locked.save(update_fields=["status", "provider_refund_id", "provider_ref", "provider_status", "provider_payload", "failure_reason", "completed_at", "updated_at"])
        return locked
