from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from notifications.events import notify_booking_disputed, notify_booking_resent
from payments.models import EscrowPayment
from support.models import SupportCase
from support.services import create_support_case

from core.state import BOOKING_TRANSITIONS, require_transition
from .models import Booking, BookingStatusHistory


def dispute_booking(*, booking_id, user, reason):
    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).select_related("request__conversation", "customer__user", "artisan__user").filter(pk=booking_id).filter(Q(customer__user_id=user.id) | Q(artisan__user_id=user.id)).first()
        if not booking:
            raise NotFound("Booking not found.")
        if booking.customer.user_id != user.id:
            raise PermissionDenied("Only the customer can dispute this booking.")
        if booking.request.requester_id != booking.customer.user_id:
            raise ValidationError({"booking": "The booking does not match its service request customer."})
        from conversations.models import Conversation
        conversation = Conversation.objects.filter(
            service_request_id=booking.request_id,
            conversation_type=Conversation.ConversationType.SERVICE,
            customer_id=booking.customer.user_id,
            artisan_id=booking.artisan_id,
        ).first()
        if not conversation:
            raise ValidationError({"conversation_id": "No matching service conversation is linked to this booking. Open the relevant request summary and continue from its conversation."})
        if booking.status not in {Booking.Status.ACCEPTED, Booking.Status.IN_PROGRESS, Booking.Status.COMPLETION_REQUESTED}:
            raise ValidationError("This booking cannot be disputed in its current state.")
        existing_case = SupportCase.objects.select_for_update(of=("self",)).filter(booking=booking, case_type=SupportCase.CaseType.DISPUTE, status__in=[SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW]).first()
        if existing_case:
            raise ValidationError("An active dispute already exists for this booking.")
        booking_status_before_dispute = booking.status
        require_transition(BOOKING_TRANSITIONS, booking.status, Booking.Status.DISPUTED, resource="booking")
        booking.status = Booking.Status.DISPUTED
        booking.save(update_fields=["status", "updated_at"])
        payments = EscrowPayment.objects.select_for_update(of=("self",)).filter(booking=booking).order_by("pk")
        for payment in payments:
            if payment.status in {EscrowPayment.Status.HELD, EscrowPayment.Status.RELEASE_PENDING}:
                payment.status = EscrowPayment.Status.DISPUTED
                payment.failure_reason = reason
                payment.save(update_fields=["status", "failure_reason", "updated_at"])
        BookingStatusHistory.objects.create(booking=booking, status=Booking.Status.DISPUTED, changed_by="CUSTOMER", changed_by_user=user, notes=reason)
        try:
            with transaction.atomic(): case = create_support_case(opened_by=user, booking=booking, conversation=conversation, case_type=SupportCase.CaseType.DISPUTE, priority=SupportCase.Priority.HIGH, subject=f"Booking dispute: {booking.pk}", description=reason, booking_status_before_dispute=booking_status_before_dispute)
        except IntegrityError as exc:
            existing_case = SupportCase.objects.filter(booking=booking, case_type=SupportCase.CaseType.DISPUTE, status__in=[SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW]).first()
            if existing_case: raise ValidationError("An active dispute already exists for this booking.") from exc
            raise
        notify_booking_disputed(booking)
        return booking, case


def resend_booking(*, booking_id, user, note=""):
    """Send a rejected (or cancelled) booking again as a NEW version.

    The previous version is never reopened: it keeps its status, its decision
    and its history. A brand new row is created in ``REQUESTED`` so the artisan
    gets a fresh ACCEPT / REJECT decision, exactly as if the agreement had just
    been sent for the first time.

    Any version of the same agreement that is still awaiting a decision is
    superseded first, so two live offers can never both be accepted.
    """
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("request", "customer__user", "artisan__user")
            .filter(pk=booking_id)
            .filter(Q(customer__user_id=user.id) | Q(artisan__user_id=user.id))
            .first()
        )
        if booking is None:
            raise NotFound("Booking not found.")

        if booking.artisan.user_id != user.id:
            raise PermissionDenied("Only the assigned artisan can send this booking again.")

        allowed, reason = booking.resend_window_state()
        if not allowed:
            raise ValidationError(reason)

        now = timezone.now()
        # Anything still awaiting a decision on this request is replaced: the
        # newest version is the only one that may be decided.
        Booking.objects.select_for_update(of=("self",)).filter(
            request_id=booking.request_id,
            status=Booking.Status.REQUESTED,
            superseded_at__isnull=True,
        ).exclude(pk=booking.pk).update(superseded_at=now, updated_at=now)

        service_request = booking.request
        if service_request.status not in {
            service_request.Status.OPEN,
            service_request.Status.ATTENDED,
        }:
            service_request.status = service_request.Status.OPEN
            service_request.save(update_fields=["status", "updated_at"])

        new_start = booking.scheduled_time_start
        # A rejected booking often carries a start time that has already passed;
        # roll it forward in whole days so the schedule keeps its shape.
        while new_start <= now:
            new_start = new_start + timedelta(days=1)
        duration = booking.scheduled_time_end - booking.scheduled_time_start

        version = Booking.objects.create(
            request=service_request,
            artisan=booking.artisan,
            customer=booking.customer,
            scheduled_time_start=new_start,
            scheduled_time_end=new_start + duration,
            price_agreed=booking.price_agreed,
            service_duration_value=booking.service_duration_value,
            service_duration_unit=booking.service_duration_unit,
            status=Booking.Status.REQUESTED,
            source=booking.source,
            resend_of=booking,
            resend_count=booking.resend_count + 1,
            resent_at=now,
            resent_by=user,
        )

        BookingStatusHistory.objects.create(
            booking=booking,
            status=booking.status,
            changed_by="ARTISAN",
            changed_by_user=user,
            notes=(note or "Sent again as a new version.")[:1000],
        )
        BookingStatusHistory.objects.create(
            booking=version,
            status=version.status,
            changed_by="ARTISAN",
            changed_by_user=user,
            notes=(
                f"Sent again (version {version.resend_count})"
                + (f": {note}" if note else "")
            )[:1000],
        )
        transaction.on_commit(lambda version=version: notify_booking_resent(version))
    return version
