from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, OpenApiTypes, extend_schema

from rest_framework import generics, status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from home.models import ArtisanProfile, CustomerProfile
from services.models import ServiceRequest
from payments.models import EscrowPayment
from payments.tasks import release_artisan_payout
from payments.services import mark_payment_release_pending
from notifications.events import *
from support.services import create_support_case
from support.models import SupportCase
from .services import dispute_booking
from core.state import require_transition, BOOKING_TRANSITIONS
from conversations.events import record_booking_event
from risk.network import record_customer_network_observation
from risk.services import record_customer_cancellation

from .models import Booking, BookingAdditionalOrder, BookingStatusHistory
from .permissions import IsBookingParticipant
from .serializers import BookingActionReasonSerializer, BookingDisputeReasonSerializer, BookingCancellationSerializer, BookingCompletionSerializer, BookingSerializer, BookingStatusHistorySerializer
from .additional_order_serializers import AdditionalOrderCreateSerializer, AdditionalOrderResponseSerializer, AdditionalOrderSerializer
from .additional_orders import create_additional_order, respond_to_additional_order, approved_addon_funding
from core.idempotency import idempotent_action

class BookingConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "booking_conflict"
    default_detail = "The booking cannot be changed because its current state does not allow this action."

class BookingPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

class BookingListCreateView(generics.ListCreateAPIView):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = BookingPagination

    def get_queryset(self):
        user = self.request.user
        queryset = Booking.objects.select_related("request__conversation", "customer__user", "artisan__user").filter(Q(customer__user_id=user.id) | Q(artisan__user_id=user.id)).order_by("-updated_at")
        booking_status = self.request.query_params.get("status")

        if booking_status:
            valid_statuses = {choice[0] for choice in Booking.STATUS_CHOICES}
            if booking_status not in valid_statuses:
                raise ValidationError({"status": f"Invalid status. Choose one of: {', '.join(sorted(valid_statuses))}."})
            queryset = queryset.filter(status=booking_status)

        return queryset.distinct()

    @extend_schema(
        tags=["Booking"],
        description="Returns bookings where the authenticated user is the customer or selected artisan. Users can only see bookings in which they participate.",
        parameters=[OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False, enum=[choice[0] for choice in Booking.STATUS_CHOICES], description="Optional booking status filter."), OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False), OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False)],
        responses={200: BookingSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["Booking"],
        summary="Retired direct booking creation",
        description="Direct customer booking creation is no longer supported. Create a service conversation, negotiate an offer, and accept it there; the booking is created from that verified conversation context.",
        request=None,
        responses={410: OpenApiResponse(description="Create the transaction through a service conversation and accepted offer.")},
    )
    def post(self, request, *args, **kwargs):
        return Response(
            {
                "code": "CHAT_TRANSACTION_REQUIRED",
                "detail": "Direct booking creation has been retired. Agree scope, price, and schedule in the service conversation and accept the offer there.",
            },
            status=status.HTTP_410_GONE,
        )

class BookingDetailView(generics.RetrieveAPIView):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated, IsBookingParticipant]

    def get_queryset(self):
        user = self.request.user
        return Booking.objects.select_related("request__conversation", "customer__user", "artisan__user").filter(Q(customer__user_id=user.id) | Q(artisan__user_id=user.id)).distinct()

    @extend_schema(
        tags=["Booking"],
        description="Returns a booking only when the authenticated user is the customer or selected artisan.",
        responses={200: BookingSerializer, 404: OpenApiResponse(description="Booking not found.")},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class BookingAdditionalOrderCreateView(APIView):
    permission_classes = [IsAuthenticated, IsBookingParticipant]

    @extend_schema(
        tags=["Booking add-ons"],
        summary="Propose a separate add-on order",
        description="Creates a separately negotiated add-on order linked to an in-progress booking. The original booking scope and price are not modified.",
        request=AdditionalOrderCreateSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={201: AdditionalOrderSerializer, 400: OpenApiResponse(description="Invalid add-on proposal."), 403: OpenApiResponse(description="Only the assigned artisan may propose add-on work."), 409: OpenApiResponse(description="The booking is not in progress.")},
    )
    @idempotent_action("booking.additional_order.create", resource_type="BookingAdditionalOrder")
    def post(self, request, booking_id):
        serializer = AdditionalOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = create_additional_order(
            booking_id=booking_id,
            user=request.user,
            amount=serializer.validated_data["amount"],
            description=serializer.validated_data["description"],
        )
        return Response(AdditionalOrderSerializer(order, context={"request": request}).data, status=status.HTTP_201_CREATED)


class BookingAdditionalOrderRespondView(APIView):
    permission_classes = [IsAuthenticated, IsBookingParticipant]

    @extend_schema(
        tags=["Booking add-ons"],
        summary="Accept, decline, or counter an add-on offer",
        request=AdditionalOrderResponseSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={200: AdditionalOrderSerializer, 400: OpenApiResponse(description="Invalid response or stale offer."), 403: OpenApiResponse(description="Only the other booking participant may respond."), 409: OpenApiResponse(description="The add-on is no longer negotiable.")},
    )
    @idempotent_action("booking.additional_order.respond", resource_type="BookingAdditionalOrder")
    def post(self, request, booking_id, order_id, offer_id):
        serializer = AdditionalOrderResponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        order, _offer = respond_to_additional_order(
            booking_id=booking_id,
            order_id=order_id,
            offer_id=offer_id,
            user=request.user,
            action=data["action"],
            amount=data.get("amount"),
            description=data.get("description"),
        )
        order = BookingAdditionalOrder.objects.select_related("booking__customer__user").prefetch_related("offers", "payment").get(pk=order.pk)
        return Response(AdditionalOrderSerializer(order, context={"request": request}).data)

def get_locked_service_request(request_id):
    try:
        return ServiceRequest.objects.select_for_update(of=("self",)).select_related("requester", "category", "location").get(pk=request_id)
    except ServiceRequest.DoesNotExist as exc:
        raise NotFound("Service request not found.") from exc

def get_locked_artisan(artisan_id):
    try:
        return ArtisanProfile.objects.select_for_update(of=("self",)).select_related("user").get(pk=artisan_id)
    except ArtisanProfile.DoesNotExist as exc:
        raise NotFound("Artisan not found.") from exc

def get_locked_booking(request, pk):
    user = request.user

    booking = (
        Booking.objects
        .select_for_update(of=("self",))
        .select_related("request", "customer__user", "artisan__user")
        .filter(Q(customer__user_id=user.id) | Q(artisan__user_id=user.id), pk=pk)
        .first()
    )

    if booking is None:
        raise NotFound("Booking not found.")

    return booking

def transition_booking(request, pk, target_status, allowed_statuses, allowed_role, serializer_class=None, note_field="reason"):
    with transaction.atomic():
        booking = get_locked_booking(request, pk)
        is_customer = booking.customer.user_id == request.user.id
        is_artisan = booking.artisan.user_id == request.user.id
        role = "CUSTOMER" if is_customer else "ARTISAN" if is_artisan else None

        if role != allowed_role:
            raise PermissionDenied(f"Only the {allowed_role.lower()} can perform this action.")

        if booking.status not in allowed_statuses:
            raise BookingConflict(f"This action is not allowed while the booking is {booking.status.lower()}.")
        require_transition(BOOKING_TRANSITIONS, booking.status, target_status, resource="booking")

        if serializer_class:
            serializer = serializer_class(data=request.data)
            serializer.is_valid(raise_exception=True)
            note = serializer.validated_data.get(note_field, "")
        else:
            note = ""

        if target_status == "ACCEPTED":
            if booking.artisan.profile_status != "ACTIVE":
                raise PermissionDenied("Your artisan profile is not currently active.")

            verification = getattr(booking.artisan, "verification", None)

            if verification is None or verification.verification_status != "VERIFIED":
                raise PermissionDenied("You must be verified before accepting bookings.")

            if booking.scheduled_time_start <= timezone.now():
                raise BookingConflict("The requested start time has already passed. Ask the customer to submit a new request.")

        booking.status = target_status
        update_fields = ["status", "updated_at"]
        if target_status in {Booking.Status.COMPLETED, Booking.Status.CANCELLED, Booking.Status.REJECTED}:
            booking.finalized_at = timezone.now()
            update_fields.append("finalized_at")
        booking.save(update_fields=update_fields)
        BookingStatusHistory.objects.create(booking=booking, status=target_status, changed_by=role, changed_by_user=request.user, notes=note)

    return booking

class BookingAcceptView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Booking"], description="Allows the selected artisan to accept a requested booking.", request=None, responses={200: BookingSerializer, 403: OpenApiResponse(description="Only the selected artisan can accept the booking."), 404: OpenApiResponse(description="Booking not found."), 409: OpenApiResponse(description="Booking cannot be accepted in its current state.")})
    @idempotent_action("booking.accept", resource_type="Booking")
    def post(self, request, pk):
        booking = transition_booking(request, pk, "ACCEPTED", ["REQUESTED"], "ARTISAN")
        notify_booking_accepted(booking)
        record_booking_event(booking=booking, event_code="BOOKING_ACCEPTED")
        return Response(BookingSerializer(booking).data)

class BookingRejectView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Booking"], description="Allows the selected artisan to reject a requested booking. A rejection reason is required.", request=BookingActionReasonSerializer, responses={200: BookingSerializer, 400: OpenApiResponse(description="A rejection reason is required."), 403: OpenApiResponse(description="Only the selected artisan can reject the booking."), 404: OpenApiResponse(description="Booking not found."), 409: OpenApiResponse(description="Booking cannot be rejected in its current state.")}, examples=[OpenApiExample("Reject Booking", value={"reason": "I am unavailable on the requested date."}, request_only=True)])
    @idempotent_action("booking.reject", resource_type="Booking")
    def post(self, request, pk):
        booking = transition_booking(request, pk, "REJECTED", ["REQUESTED"], "ARTISAN", BookingActionReasonSerializer)
        notify_booking_rejected(booking)
        record_booking_event(booking=booking, event_code="BOOKING_REJECTED")
        return Response(BookingSerializer(booking).data)

class BookingCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Bookings"],
        description="Allows the customer to cancel a requested or accepted booking. The customer cancellation reason is classified into a severity score, contributing to a rolling 90-day risk score capped at 5. Reaching 5/5 automatically suspends the account and may place high-confidence observed IPs into a temporary authentication and booking network restriction.",
        request=BookingCancellationSerializer,
        responses={
            200: BookingSerializer,
            400: OpenApiResponse(description="Cancellation reason is required."),
            403: OpenApiResponse(description="Only the customer can cancel the booking."),
            404: OpenApiResponse(description="Booking not found."),
            409: OpenApiResponse(description="Booking cannot be cancelled in its current state."),
        },
        examples=[
            OpenApiExample(
                "Cancel Booking",
                value={"reason_code": "CUSTOMER_CHANGED_MIND", "reason": "My plans have changed."},
                request_only=True,
            )
        ],
    )
    @idempotent_action("booking.cancel", resource_type="Booking")
    def post(self, request, pk):
        serializer = BookingCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            booking = get_locked_booking(request, pk)

            if booking.customer.user_id != request.user.id:
                raise PermissionDenied("Only the customer can cancel this booking.")

            if booking.status not in {"REQUESTED", "ACCEPTED"}:
                raise BookingConflict("Booking cannot be cancelled in its current state.")

            previous_status = booking.status
            payment = EscrowPayment.objects.select_for_update(of=("self",)).filter(booking=booking, addon_order__isnull=True).first()

            if payment and payment.status not in {
                EscrowPayment.Status.FAILED,
                EscrowPayment.Status.REFUNDED,
            }:
                raise BookingConflict(
                    "This booking has an associated payment and must use the payment/dispute process."
                )

            booking.status = Booking.Status.CANCELLED
            booking.finalized_at = timezone.now()
            booking.save(update_fields=["status", "finalized_at", "updated_at"])

            BookingStatusHistory.objects.create(
                booking=booking,
                status="CANCELLED",
                changed_by="CUSTOMER",
                changed_by_user=request.user,
                notes=serializer.validated_data["reason"],
            )

            profile, suspended = record_customer_cancellation(
                booking=booking,
                reason_code=serializer.validated_data.get("reason_code", "OTHER"),
                reason_text=serializer.validated_data["reason"],
                request=request,
                previous_status=previous_status,
            )

        notify_booking_cancelled(booking)
        record_booking_event(booking=booking, event_code="BOOKING_CANCELLED")
        response = BookingSerializer(booking).data
        response["cancellation_risk"] = {
            "score": profile.cancellation_points,
            "max_score": 5,
            "weighted_cancellation_rate": profile.weighted_cancellation_rate,
            "risk_level": profile.risk_level,
            "account_suspended": suspended,
        }
        return Response(response)

class BookingStartView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Bookings"], summary="Start booking", description="Allows the assigned artisan to start an accepted booking only after the customer's payment has been verified and placed on hold.", request=None, responses={200: BookingSerializer})
    @idempotent_action("booking.start", resource_type="Booking")
    def post(self, request, pk):
        with transaction.atomic():
            booking = get_locked_booking(request, pk)

            if booking.artisan.user_id != request.user.id:
                raise PermissionDenied("Only the assigned artisan can start this booking.")

            if booking.status != "ACCEPTED":
                raise BookingConflict("Booking must be accepted before it can start.")

            payment = EscrowPayment.objects.select_for_update(of=("self",)).filter(booking=booking, addon_order__isnull=True).first()

            if payment is None:
                raise BookingConflict("Customer payment has not been initialized.")

            if payment.status != EscrowPayment.Status.HELD:
                raise BookingConflict("Customer payment has not been verified and held.")

            booking.status = "IN_PROGRESS"
            booking.save(update_fields=["status", "updated_at"])

            BookingStatusHistory.objects.create(booking=booking, status="IN_PROGRESS", changed_by="ARTISAN", changed_by_user=request.user, notes="Artisan started the service.")
            notify_booking_started(booking)
            record_booking_event(booking=booking, event_code="SERVICE_STARTED")

        return Response(BookingSerializer(booking).data)

class BookingCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Bookings"], summary="Submit booking completion", description="Allows the assigned artisan to submit completion. This does not release payment. The customer must confirm completion separately.", request=BookingCompletionSerializer, responses={200: BookingSerializer})
    @idempotent_action("booking.complete", resource_type="Booking")
    def post(self, request, pk):
        serializer = BookingCompletionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            booking = get_locked_booking(request, pk)

            if booking.artisan.user_id != request.user.id:
                raise PermissionDenied("Only the assigned artisan can complete this booking.")

            if booking.status != "IN_PROGRESS":
                raise BookingConflict("Booking must be in progress before completion.")

            if BookingAdditionalOrder.objects.filter(booking=booking, status=BookingAdditionalOrder.Status.NEGOTIATING).exists():
                raise BookingConflict("Resolve every open add-on order before submitting service completion.")
            all_addons_funded, _addon_payments, _unpaid_orders = approved_addon_funding(booking, lock=True)
            if not all_addons_funded:
                raise BookingConflict("Every approved add-on order must be paid and confirmed in escrow before service completion.")

            booking.artisan_completed_at = timezone.now()
            booking.status = "COMPLETION_REQUESTED"

            booking.save(
                update_fields=[
                    "artisan_completed_at",
                    "status",
                    "updated_at",
                ]
            )

            BookingStatusHistory.objects.create(
                booking=booking,
                status="COMPLETION_REQUESTED",
                changed_by="ARTISAN",
                changed_by_user=request.user,
                notes=serializer.validated_data.get("note", ""),
            )
            notify_artisan_completed(booking)
            record_booking_event(booking=booking, event_code="COMPLETION_REQUESTED")

        return Response(BookingSerializer(booking).data)

class BookingCustomerCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Bookings"],
        summary="Confirm booking completion",
        description="Allows the customer to confirm a completion request. Successful confirmation moves the payment into the payout release process.",
        request=None,
        responses={
            200: BookingSerializer,
            403: OpenApiResponse(description="Only the customer can confirm completion."),
            404: OpenApiResponse(description="Booking not found."),
            409: OpenApiResponse(description="Booking is not awaiting customer confirmation."),
        },
    )
    @idempotent_action("booking.confirm_completion", resource_type="Booking")
    def post(self, request, pk):
        with transaction.atomic():
            booking = get_locked_booking(request, pk)

            if booking.customer.user_id != request.user.id:
                raise PermissionDenied("Only the customer can confirm completion.")

            if booking.status != "COMPLETION_REQUESTED":
                raise BookingConflict("Booking is not awaiting customer confirmation.")

            if not booking.artisan_completed_at:
                raise BookingConflict("Artisan completion has not been submitted.")

            base_payment = EscrowPayment.objects.select_for_update(of=("self",)).filter(
                booking=booking, addon_order__isnull=True
            ).first()
            if base_payment is None or base_payment.status != EscrowPayment.Status.HELD:
                raise BookingConflict("Original booking payment is not currently held.")

            all_addons_funded, addon_payments, _unpaid_orders = approved_addon_funding(booking, lock=True)
            if not all_addons_funded:
                raise BookingConflict("Every approved add-on order must be paid and confirmed in escrow before completion.")

            now = timezone.now()
            booking.customer_completed_at = now
            booking.payout_eligible_at = now
            booking.finalized_at = now
            booking.status = Booking.Status.COMPLETED
            booking.save(update_fields=["customer_completed_at", "payout_eligible_at", "finalized_at", "status", "updated_at"])

            payments_to_release = [base_payment, *addon_payments]
            for payment in payments_to_release:
                mark_payment_release_pending(payment.id)
                payment_id = str(payment.id)
                transaction.on_commit(lambda payment_id=payment_id: release_artisan_payout.delay(payment_id), robust=True)

            BookingStatusHistory.objects.create(booking=booking, status="COMPLETED", changed_by="CUSTOMER", changed_by_user=request.user, notes="Customer confirmed service completion.")

            notify_customer_completed(booking)
            record_booking_event(booking=booking, event_code="COMPLETION_ACCEPTED")
            for payment in payments_to_release:
                record_booking_event(
                    booking=booking,
                    event_code="RELEASE_PENDING",
                    event_key=f"payment:{payment.id}:release_pending",
                    metadata={"payment_id": str(payment.id), "additional_order_id": str(payment.addon_order_id) if payment.addon_order_id else None},
                )

        return Response(BookingSerializer(booking).data)

class BookingDisputeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Bookings"],
        summary="Dispute booking",
        description="Allows the customer to dispute an eligible booking. The booking becomes disputed, the relevant payout remains frozen, and a customer-care support case is created.",
        request=BookingDisputeReasonSerializer,
        responses={200: BookingSerializer},
    )
    @idempotent_action("booking.dispute", resource_type="Booking")
    def post(self, request, pk):
        serializer = BookingDisputeReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking, case = dispute_booking(booking_id=pk, user=request.user, reason=serializer.validated_data["reason"])
        record_booking_event(booking=booking, event_code="DISPUTE_OPENED", event_key=f"booking:{booking.id}:disputed", metadata={"booking_id": str(booking.id), "case_id": case.id})
        return Response(BookingSerializer(booking).data)

class BookingHistoryView(generics.ListAPIView):
    serializer_class = BookingStatusHistorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return BookingStatusHistory.objects.none()
        user = self.request.user
        booking = Booking.objects.filter(Q(customer__user_id=user.id) | Q(artisan__user_id=user.id), pk=self.kwargs["pk"]).first()

        if booking is None:
            raise NotFound("Booking not found.")

        return booking.status_history.select_related("changed_by_user").order_by("created_at")

    @extend_schema(tags=["Booking"], description="Returns the complete status transition history for a booking accessible to the authenticated customer or artisan.", responses={200: BookingStatusHistorySerializer(many=True), 404: OpenApiResponse(description="Booking not found.")})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
