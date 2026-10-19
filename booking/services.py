from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from notifications.events import notify_booking_disputed
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
