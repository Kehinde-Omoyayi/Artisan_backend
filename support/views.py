from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import ( OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema, )

from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import audit_request_action
from core.idempotency import idempotent_action
from home.models import StaffPasswordResetToken, StaffSecurityChallenge, StaffSecurityProfile
from rest_framework.throttling import ScopedRateThrottle

from .models import SupportCase, SupportCaseEvent, SupportFinancialAction
from payments.models import EscrowPayment
from promotions.models import PromotionPayment

from .permissions import ( ALL_SUPPORT_ROLES, SUPPORT_ADMIN, SUPPORT_AGENT, SUPPORT_FINANCE, SUPPORT_SUPERVISOR, 
    SupportAgentPermission, SupportAdminPermission, SupportFinancePermission, SupportRolePermission, 
    SupportSupervisorPermission, user_has_support_role, user_support_roles, )

from .serializers import ( SupportCapabilitiesSerializer, SupportCaseAssignSerializer, SupportCaseCreateSerializer,
    SupportCaseEventSerializer, SupportCaseMessageSerializer, SupportCaseNoteSerializer,
    SupportCaseRefundEligiblePaymentSerializer, SupportCaseResolveSerializer, SupportCaseSerializer,
    SupportFinancialActionFinanceSerializer, SupportFinancialActionPublicSerializer,
    SupportFinancialActionSerializer, SupportRefundApprovalSerializer, SupportRefundRequestSerializer,
    SupportRefundRejectionSerializer, SupportRoleUpdateSerializer, SupportStaffSerializer, )

from .services import ( _refundable_amount_kobo, add_case_note, send_case_message, approve_refund,
    reject_refund, assign_support_case, claim_support_case, close_support_case,
    create_refund_request, create_support_case, execute_refund, reconcile_refund,
    resolve_support_case, )

User = get_user_model()


class SupportCapabilitiesView(APIView):
    """Expose the signed-in operator's capabilities for permission-aware UI."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Support"],
        responses={200: SupportCapabilitiesSerializer},
    )
    def get(self, request):
        user = request.user

        def has_role(allowed_roles=None):
            return user_has_support_role(user, allowed_roles)

        roles = user_support_roles(user)

        return Response({
            "is_support_staff": has_role(),
            "is_superuser": bool(user.is_superuser),
            "roles": roles,
            "can_claim_cases": has_role({SUPPORT_AGENT, SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_assign_cases": has_role({SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_add_private_notes": has_role(),
            "can_resolve_cases": has_role({SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_close_cases": has_role({SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_view_refunds": has_role(),
            "can_request_refunds": has_role({SUPPORT_AGENT, SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_approve_refunds": has_role({SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_execute_refunds": has_role({SUPPORT_FINANCE, SUPPORT_ADMIN}),
            "can_reconcile_refunds": has_role({SUPPORT_FINANCE, SUPPORT_ADMIN}),
            "can_manage_staff": has_role({SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
            "can_manage_roles": has_role({SUPPORT_ADMIN}),
            "can_view_finance": has_role({SUPPORT_FINANCE, SUPPORT_ADMIN}),
            "can_review_promotions": has_role({SUPPORT_AGENT, SUPPORT_SUPERVISOR, SUPPORT_ADMIN}),
        })


class SupportCaseQueryMixin:

    def support_queryset(self):
        return SupportCase.objects.select_related(
            "booking__customer__user",
            "booking__artisan__user",
            "booking__request__conversation__customer",
            "booking__request__conversation__artisan__user",
            "conversation__customer",
            "conversation__artisan__user",
            "promotion_campaign__artisan__user",
            "opened_by",
            "assigned_to",
        )

    def participant_queryset(self, user):
        return self.support_queryset().filter(
            Q(opened_by=user)
            | Q(booking__customer__user_id=user.id)
            | Q(booking__artisan__user_id=user.id)
            | Q(conversation__customer_id=user.id)
            | Q(conversation__artisan__user_id=user.id)
            | Q(conversation__participants=user)
            | Q(promotion_campaign__artisan__user_id=user.id)
        ).distinct()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SupportCase.objects.none()
        user = self.request.user

        if SupportRolePermission().has_permission(
            self.request,
            self,
        ):
            queryset = self.support_queryset()

            search = str(self.request.query_params.get("search") or "").strip()
            if search:
                queryset = queryset.filter(
                    Q(reference__icontains=search)
                    | Q(subject__icontains=search)
                    | Q(description__icontains=search)
                    | Q(opened_by__email__icontains=search)
                    | Q(assigned_to__email__icontains=search)
                )

            assigned_to = self.request.query_params.get("assigned_to")
            status_filter = self.request.query_params.get("status")
            priority = self.request.query_params.get("priority")
            case_type = self.request.query_params.get("case_type")

            if assigned_to == "me":
                queryset = queryset.filter(assigned_to=user)

            elif assigned_to == "unassigned":
                queryset = queryset.filter(
                    assigned_to__isnull=True
                )

            elif assigned_to:
                queryset = queryset.filter(
                    assigned_to_id=assigned_to
                )

            if status_filter:
                queryset = queryset.filter(status=status_filter)

            if priority:
                queryset = queryset.filter(priority=priority)

            if case_type:
                queryset = queryset.filter(case_type=case_type)

            campaign_id = str(self.request.query_params.get("promotion_campaign") or "").strip()
            if campaign_id:
                try:
                    campaign_id = UUID(campaign_id)
                except (TypeError, ValueError):
                    raise ValidationError({"promotion_campaign": "Enter a valid promotion campaign ID."}) from None
                queryset = queryset.filter(promotion_campaign_id=campaign_id)

            return queryset

        queryset = self.participant_queryset(user)
        campaign_id = str(self.request.query_params.get("promotion_campaign") or "").strip()
        if campaign_id:
            try:
                campaign_id = UUID(campaign_id)
            except (TypeError, ValueError):
                raise ValidationError({"promotion_campaign": "Enter a valid promotion campaign ID."}) from None
            queryset = queryset.filter(promotion_campaign_id=campaign_id)
        search = str(self.request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(subject__icontains=search)
                | Q(description__icontains=search)
            )
        return queryset


class SupportCaseEventsView(
    SupportCaseQueryMixin,
    generics.ListAPIView,
):
    serializer_class = SupportCaseEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SupportCaseEvent.objects.none()
        case = get_object_or_404(
            self.get_queryset_for_case(),
            pk=self.kwargs["pk"],
        )

        events = case.events.select_related(
            "performed_by"
        ).all()

        if not SupportRolePermission().has_permission(
            self.request,
            self,
        ):
            events = events.exclude(action="CASE_NOTE_ADDED")

        return events

    def get_queryset_for_case(self):
        user = self.request.user

        if SupportRolePermission().has_permission(
            self.request,
            self,
        ):
            return self.support_queryset()

        return self.participant_queryset(user)

    @extend_schema(
        tags=["Support Cases"],
        responses={
            200: SupportCaseEventSerializer(many=True),
            404: OpenApiResponse(
                description="Support case not found."
            ),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SupportCaseListCreateView(
    SupportCaseQueryMixin,
    generics.ListCreateAPIView,
):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_create"

    def get_serializer_class(self):
        if self.request.method == "POST":
            return SupportCaseCreateSerializer

        return SupportCaseSerializer

    @extend_schema(
        tags=["Support Cases"],
        description=(
            "Support staff receive the operational queue. "
            "Customers and artisans receive only cases they "
            "participate in."
        ),
        parameters=[
            OpenApiParameter(
                "search",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Search visible case references, subjects, descriptions, and requester email.",
            ),
            OpenApiParameter(
                "assigned_to",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                "priority",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                "case_type",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                "promotion_campaign",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Filter support cases by the exact promotion campaign UUID.",
            ),
        ],
        responses={200: SupportCaseSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["Support Cases"],
        request=SupportCaseCreateSerializer,
        responses={
            201: SupportCaseSerializer,
            400: OpenApiResponse(
                description="Invalid case data."
            ),
            403: OpenApiResponse(
                description=(
                    "The user does not participate in the supplied booking."
                )
            ),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        case = create_support_case(
            opened_by=request.user,
            booking=serializer.validated_data.get("booking"),
            conversation=serializer.validated_data.get("conversation"),
            promotion_campaign=serializer.validated_data.get("promotion_campaign"),
            case_type=serializer.validated_data["case_type"],
            priority=serializer.validated_data["priority"],
            subject=serializer.validated_data["subject"],
            description=serializer.validated_data["description"],
        )

        return Response(
            SupportCaseSerializer(case, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class SupportCaseDetailView(generics.RetrieveAPIView):
    serializer_class = SupportCaseSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SupportCase.objects.none()
        user = self.request.user

        if SupportRolePermission().has_permission(
            self.request,
            self,
        ):
            return SupportCase.objects.select_related(
                "booking__customer__user",
                "booking__artisan__user",
                "booking__request__conversation__customer",
                "booking__request__conversation__artisan__user",
                "conversation__customer",
                "conversation__artisan__user",
                "promotion_campaign__artisan__user",
                "opened_by",
                "assigned_to",
            )

        return (
            SupportCase.objects
            .select_related(
                "booking__customer__user",
                "booking__artisan__user",
                "booking__request__conversation__customer",
                "booking__request__conversation__artisan__user",
                "conversation__customer",
                "conversation__artisan__user",
                "promotion_campaign__artisan__user",
                "opened_by",
                "assigned_to",
            )
            .filter(
                Q(opened_by=user)
                | Q(booking__customer__user_id=user.id)
                | Q(booking__artisan__user_id=user.id)
                | Q(conversation__customer_id=user.id)
                | Q(conversation__artisan__user_id=user.id)
                | Q(conversation__participants=user)
                | Q(promotion_campaign__artisan__user_id=user.id)
            )
            .distinct()
        )

    @extend_schema(
        tags=["Support Cases"],
        responses={
            200: SupportCaseSerializer,
            404: OpenApiResponse(
                description="Case not found."
            ),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SupportCaseEligiblePaymentsView(APIView):
    """List refundable booking payments without exposing provider payloads."""

    permission_classes = [SupportAgentPermission]

    @extend_schema(
        tags=["Support Refunds"],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        case = get_object_or_404(
            SupportCase.objects.select_related("booking", "promotion_campaign"),
            pk=pk,
        )

        if (
            case.status not in {SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW}
            or case.case_type not in {SupportCase.CaseType.DISPUTE, SupportCase.CaseType.PAYMENT}
        ):
            return Response({"count": 0, "results": []})

        if case.promotion_campaign_id:
            payments = PromotionPayment.objects.filter(
                campaign_id=case.promotion_campaign_id,
                status__in=[PromotionPayment.Status.VERIFIED, PromotionPayment.Status.PARTIALLY_REFUNDED, PromotionPayment.Status.REVIEW_REQUIRED],
            ).exclude(provider_transaction_id__isnull=True).exclude(provider_transaction_id="").order_by("created_at")
            results = []
            for payment in payments:
                remaining_kobo = _refundable_amount_kobo(payment)
                if remaining_kobo < 10_000 or remaining_kobo % 100:
                    continue
                results.append({
                    "id": str(payment.id),
                    "reference": payment.reference,
                    "status": payment.status,
                    "currency": payment.currency,
                    "total_amount": Decimal(payment.amount_kobo) / Decimal("100"),
                    "remaining_amount": Decimal(remaining_kobo) / Decimal("100"),
                })
            return Response({"count": len(results), "results": SupportCaseRefundEligiblePaymentSerializer(results, many=True).data})

        if not case.booking_id:
            return Response({"count": 0, "results": []})

        payments = (
            EscrowPayment.objects
            .filter(
                booking_id=case.booking_id,
                status__in=[EscrowPayment.Status.HELD, EscrowPayment.Status.DISPUTED],
            )
            .exclude(provider_transaction_id__isnull=True)
            .exclude(provider_transaction_id="")
            .order_by("created_at")
        )

        results = []
        for payment in payments:
            remaining_kobo = _refundable_amount_kobo(payment)
            # Flutterwave's refund endpoint accepts whole NGN amounts only.
            if remaining_kobo < 10_000 or remaining_kobo % 100:
                continue
            results.append({
                "id": str(payment.id),
                "reference": payment.reference,
                "status": payment.status,
                "currency": "NGN",
                "total_amount": Decimal(payment.total_amount_kobo) / Decimal("100"),
                "remaining_amount": Decimal(remaining_kobo) / Decimal("100"),
            })

        return Response({
            "count": len(results),
            "results": SupportCaseRefundEligiblePaymentSerializer(results, many=True).data,
        })


class SupportCaseClaimView(APIView):
    permission_classes = [SupportAgentPermission]

    @extend_schema(
        tags=["Support Cases"],
        request=None,
        responses={
            200: SupportCaseSerializer,
            409: OpenApiResponse(
                description="Case cannot be claimed."
            ),
        },
    )
    def post(self, request, pk):
        with transaction.atomic():
            case = get_object_or_404(
                SupportCase.objects.select_for_update(of=("self",)),
                pk=pk,
            )

            try:
                claim_support_case(
                    case=case,
                    staff_user=request.user,
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(SupportCaseSerializer(case).data)


class SupportCaseAssignView(APIView):
    permission_classes = [SupportSupervisorPermission]

    @extend_schema(
        tags=["Support Cases"],
        request=SupportCaseAssignSerializer,
        responses={
            200: SupportCaseSerializer,
            400: OpenApiResponse(
                description="Invalid staff assignment."
            ),
            404: OpenApiResponse(
                description=(
                    "Support case or support user not found."
                )
            ),
            409: OpenApiResponse(
                description=(
                    "Case cannot be assigned in its current state."
                )
            ),
        },
    )
    def post(self, request, pk):
        serializer = SupportCaseAssignSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        staff_user = User.objects.filter(
            pk=serializer.validated_data["staff_user_id"],
            is_staff=True,
            is_active=True,
        ).first()

        if (
            staff_user is None
            or not SupportRolePermission().has_permission_for_user(
                staff_user
            )
        ):
            return Response(
                {"detail": "Support user not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            case = (
                SupportCase.objects
                .select_for_update(of=("self",))
                .filter(pk=pk)
                .first()
            )

            if case is None:
                return Response(
                    {"detail": "Support case not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            try:
                assign_support_case(
                    case=case,
                    staff_user=staff_user,
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(SupportCaseSerializer(case).data)


class SupportCaseNoteView(APIView):
    permission_classes = [SupportAgentPermission]

    @extend_schema(
        tags=["Support Cases"],
        request=SupportCaseNoteSerializer,
        responses={
            200: SupportCaseSerializer,
            409: OpenApiResponse(
                description="Case is closed."
            ),
        },
    )
    def post(self, request, pk):
        serializer = SupportCaseNoteSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            case = get_object_or_404(
                SupportCase.objects.select_for_update(of=("self",)),
                pk=pk,
            )

            try:
                add_case_note(
                    case=case,
                    staff_user=request.user,
                    note=serializer.validated_data["note"],
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(SupportCaseSerializer(case).data)


class SupportCaseMessageView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_message"

    @extend_schema(
        tags=["Support Cases"],
        summary="Send a public message on a support case",
        description="Allows a booking participant or support staff member to add a public, auditable message to the case thread. Internal support notes remain private.",
        request=SupportCaseMessageSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={
            201: SupportCaseEventSerializer,
            400: OpenApiResponse(description="Invalid message."),
            403: OpenApiResponse(description="Only a case participant or support staff can send a message."),
            404: OpenApiResponse(description="Support case not found."),
            409: OpenApiResponse(description="Case is resolved or closed."),
        },
    )
    @idempotent_action("support.case.message", resource_type="SupportCaseEvent")
    def post(self, request, pk):
        serializer = SupportCaseMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        case_queryset = SupportCase.objects.select_related(
            "opened_by", "assigned_to", "booking__customer__user", "booking__artisan__user", "promotion_campaign__artisan__user"
        )
        if not SupportRolePermission().has_permission(request, self):
            case_queryset = case_queryset.filter(
                Q(opened_by_id=request.user.id)
                | Q(booking__customer__user_id=request.user.id)
                | Q(booking__artisan__user_id=request.user.id)
                | Q(promotion_campaign__artisan__user_id=request.user.id)
            ).distinct()

        with transaction.atomic():
            case = get_object_or_404(
                case_queryset.select_for_update(of=("self",)),
                pk=pk,
            )
            try:
                event = send_case_message(
                    case=case,
                    sender=request.user,
                    message=serializer.validated_data["message"],
                )
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(
            SupportCaseEventSerializer(event, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class SupportCaseResolveView(APIView):
    permission_classes = [SupportSupervisorPermission]

    @extend_schema(
        tags=["Support Cases"],
        request=SupportCaseResolveSerializer,
        responses={
            200: SupportCaseSerializer,
            409: OpenApiResponse(
                description="Invalid resolution."
            ),
        },
    )
    def post(self, request, pk):
        serializer = SupportCaseResolveSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            case = get_object_or_404(
                SupportCase.objects.select_for_update(of=("self",)),
                pk=pk,
            )

            try:
                case = resolve_support_case(
                    case=case,
                    staff_user=request.user,
                    resolution_code=serializer.validated_data[
                        "resolution_code"
                    ],
                    resolution_note=serializer.validated_data[
                        "resolution_note"
                    ],
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(SupportCaseSerializer(case).data)


class SupportCaseCloseView(APIView):
    permission_classes = [SupportSupervisorPermission]

    @extend_schema(
        tags=["Support Cases"],
        request=None,
        responses={
            200: SupportCaseSerializer,
            409: OpenApiResponse(
                description=(
                    "Case must be resolved before closing."
                )
            ),
        },
    )
    def post(self, request, pk):
        with transaction.atomic():
            case = get_object_or_404(
                SupportCase.objects.select_for_update(of=("self",)),
                pk=pk,
            )

            try:
                case = close_support_case(
                    case=case,
                    staff_user=request.user,
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(SupportCaseSerializer(case).data)


class SupportRefundRequestView(APIView):
    permission_classes = [SupportAgentPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_refund_request"

    @extend_schema(
        tags=["Support Refunds"],
        request=SupportRefundRequestSerializer,
        responses={
            201: SupportFinancialActionSerializer,
            409: OpenApiResponse(
                description="Refund cannot be requested."
            ),
            404: OpenApiResponse(
                description="Case or payment not found."
            ),
        },
        parameters=[
            OpenApiParameter(
                "Idempotency-Key",
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                required=False,
            )
        ],
        examples=[
            OpenApiExample(
                "Request full refund",
                value={
                    "payment_id": (
                        "00000000-0000-0000-0000-000000000000"
                    ),
                    "amount": "10150.00",
                    "reason": (
                        "Booking was cancelled after a verified "
                        "service failure."
                    ),
                    "idempotency_key": "refund-01-customer-case-01",
                },
                request_only=True,
            )
        ],
    )
    @idempotent_action("support.refund.request", resource_type="SupportFinancialAction")
    def post(self, request, pk):
        payload = request.data.copy()

        if not payload.get("idempotency_key"):
            header_key = request.headers.get("Idempotency-Key")

            if header_key:
                payload["idempotency_key"] = header_key

        serializer = SupportRefundRequestSerializer(
            data=payload
        )
        serializer.is_valid(raise_exception=True)

        idempotency_key = serializer.validated_data.get(
            "idempotency_key"
        )

        if not idempotency_key:
            return Response(
                {
                    "detail": (
                        "Idempotency-Key header or idempotency_key "
                        "body field is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            case = get_object_or_404(
                SupportCase.objects.select_for_update(of=("self",)),
                pk=pk,
            )

            payment = None
            promotion_payment = None
            if case.promotion_campaign_id:
                promotion_payment = get_object_or_404(
                    PromotionPayment.objects.select_for_update(of=("self",)),
                    pk=serializer.validated_data["payment_id"],
                    campaign_id=case.promotion_campaign_id,
                )
            else:
                payment = get_object_or_404(
                    EscrowPayment.objects.select_for_update(of=("self",)),
                    pk=serializer.validated_data["payment_id"],
                )

            try:
                action = create_refund_request(
                    case=case,
                    staff_user=request.user,
                    payment=payment,
                    promotion_payment=promotion_payment,
                    amount=serializer.validated_data["amount"],
                    reason=serializer.validated_data["reason"],
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(
            SupportFinancialActionSerializer(action).data,
            status=status.HTTP_201_CREATED,
        )


class SupportRefundListView(generics.ListAPIView):
    permission_classes = [SupportRolePermission]

    queryset = (
        SupportFinancialAction.objects
        .select_related(
            "case",
            "payment",
            "requested_by",
            "approved_by",
            "executed_by",
        )
        .all()
    )

    filterset_fields = ["status", "action_type", "case"]
    ordering_fields = [
        "requested_at",
        "approved_at",
        "completed_at",
    ]
    ordering = ["-requested_at"]

    def get_serializer_class(self):
        roles = user_support_roles(self.request.user)

        if (
            SUPPORT_FINANCE in roles
            or SUPPORT_ADMIN in roles
            or self.request.user.is_superuser
        ):
            return SupportFinancialActionFinanceSerializer

        return SupportFinancialActionSerializer

    @extend_schema(
        tags=["Support Refunds"],
        responses={
            200: SupportFinancialActionSerializer(many=True)
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SupportRefundApproveView(APIView):
    permission_classes = [SupportSupervisorPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_refund_approve"

    @extend_schema(
        tags=["Support Refunds"],
        request=SupportRefundApprovalSerializer,
        responses={
            200: SupportFinancialActionSerializer,
            409: OpenApiResponse(
                description="Refund cannot be approved."
            ),
        },
    )
    @idempotent_action("support.refund.approve", resource_type="SupportFinancialAction")
    def post(self, request, refund_id):
        serializer = SupportRefundApprovalSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            action = get_object_or_404(
                SupportFinancialAction.objects
                .select_for_update(of=("self",))
                .select_related("case", "payment__artisan"),
                pk=refund_id,
            )

            try:
                approve_refund(
                    action=action,
                    staff_user=request.user,
                    approval_note=serializer.validated_data[
                        "approval_note"
                    ],
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(
            SupportFinancialActionSerializer(action).data
        )


class SupportRefundRejectView(APIView):
    permission_classes = [SupportSupervisorPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_refund_reject"

    @extend_schema(
        tags=["Support Refunds"],
        request=SupportRefundRejectionSerializer,
        responses={
            200: SupportFinancialActionSerializer,
            409: OpenApiResponse(
                description="Refund cannot be rejected."
            ),
        },
    )
    @idempotent_action("support.refund.reject", resource_type="SupportFinancialAction")
    def post(self, request, refund_id):
        serializer = SupportRefundRejectionSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            action = get_object_or_404(
                SupportFinancialAction.objects
                .select_for_update(of=("self",))
                .select_related("case", "payment__artisan"),
                pk=refund_id,
            )

            try:
                reject_refund(
                    action=action,
                    staff_user=request.user,
                    rejection_note=serializer.validated_data[
                        "rejection_note"
                    ],
                )
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response(
            SupportFinancialActionSerializer(action).data
        )


class SupportRefundExecuteView(APIView):
    permission_classes = [SupportFinancePermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_refund_execute"

    @extend_schema(
        tags=["Support Refunds"],
        request=None,
        responses={
            200: SupportFinancialActionFinanceSerializer,
            409: OpenApiResponse(
                description="Refund cannot be executed."
            ),
        },
    )
    @idempotent_action("support.refund.execute", resource_type="SupportFinancialAction", atomic=False)
    def post(self, request, refund_id):
        action = get_object_or_404(
            SupportFinancialAction.objects.select_related(
                "case",
                "payment__artisan",
            ),
            pk=refund_id,
        )

        try:
            action = execute_refund(
                action=action,
                staff_user=request.user,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            SupportFinancialActionFinanceSerializer(action).data
        )


class SupportRefundReconcileView(APIView):
    permission_classes = [SupportFinancePermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_refund_reconcile"

    @extend_schema(
        tags=["Support Refunds"],
        request=None,
        responses={
            200: SupportFinancialActionFinanceSerializer,
            409: OpenApiResponse(
                description="Refund cannot be reconciled."
            ),
        },
    )
    @idempotent_action("support.refund.reconcile", resource_type="SupportFinancialAction", atomic=False)
    def post(self, request, refund_id):
        action = get_object_or_404(
            SupportFinancialAction.objects.select_related(
                "case",
                "payment__artisan",
            ),
            pk=refund_id,
        )

        try:
            action = reconcile_refund(action=action)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            SupportFinancialActionFinanceSerializer(action).data
        )


class SupportRefundDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Support Refunds"],
        responses={
            200: SupportFinancialActionSerializer,
            404: OpenApiResponse(
                description="Refund not found."
            ),
        },
    )
    def get(self, request, refund_id):
        action = get_object_or_404(
            SupportFinancialAction.objects.select_related(
                "case",
                "payment__artisan",
                "payment__customer",
                "promotion_payment__campaign__artisan__user",
            ),
            pk=refund_id,
        )

        support_access = SupportRolePermission().has_permission(request, self)
        participant_access = action.case.opened_by_id == request.user.id
        if action.payment_id:
            participant_access = participant_access or action.payment.customer_id == request.user.id or action.payment.artisan.user_id == request.user.id
        if action.promotion_payment_id:
            participant_access = participant_access or action.promotion_payment.campaign.artisan.user_id == request.user.id

        if not support_access and not participant_access:
            raise PermissionDenied(
                "You are not authorized to view this refund."
            )

        if support_access:
            roles = user_support_roles(request.user)

            serializer_class = (
                SupportFinancialActionFinanceSerializer
                if (
                    SUPPORT_FINANCE in roles
                    or SUPPORT_ADMIN in roles
                    or request.user.is_superuser
                )
                else SupportFinancialActionSerializer
            )

            return Response(serializer_class(action).data)

        return Response(
            SupportFinancialActionPublicSerializer(action).data
        )


class SupportStaffListView(APIView):
    permission_classes = [SupportSupervisorPermission]

    @extend_schema(
        tags=["Support Administration"],
        responses={200: SupportStaffSerializer(many=True)},
    )
    def get(self, request):
        users = (
            User.objects
            .filter(
                is_staff=True,
                is_active=True,
                groups__name__in=ALL_SUPPORT_ROLES,
            )
            .distinct()
            .order_by("email")
        )

        users = list(users)
        registered_security_users = set()
        if request.user.is_superuser:
            registered_security_users = set(
                StaffSecurityProfile.objects.filter(user_id__in=[user.pk for user in users])
                .values_list("user_id", flat=True)
            )

        data = []
        for user in users:
            row = {
                "id": user.id,
                "email": user.email,
                "name": user.get_full_name() or user.email,
                "roles": user_support_roles(user),
            }
            if request.user.is_superuser:
                row["security_questions_registered"] = user.pk in registered_security_users
            data.append(row)

        return Response(
            SupportStaffSerializer(data, many=True).data
        )


class SupportStaffRoleView(APIView):
    permission_classes = [SupportAdminPermission]

    @extend_schema(
        tags=["Support Administration"],
        request=SupportRoleUpdateSerializer,
        responses={
            200: SupportStaffSerializer,
            400: OpenApiResponse(
                description="Invalid support role assignment."
            ),
        },
    )
    def put(self, request, user_id):
        serializer = SupportRoleUpdateSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        roles = set(serializer.validated_data["roles"])
        with transaction.atomic():
            target = get_object_or_404(User.objects.select_for_update(), pk=user_id)
            if target.is_superuser:
                raise PermissionDenied(
                    "Superuser roles cannot be changed through customer support role management."
                )
            if not target.is_staff or not target.is_active:
                return Response(
                    {"detail": "Support roles can only be assigned to active staff users."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            current_groups = set(
                target.groups.filter(name__in=ALL_SUPPORT_ROLES)
                .values_list("name", flat=True)
            )
            groups = {
                role: Group.objects.get_or_create(name=role)[0]
                for role in ALL_SUPPORT_ROLES
            }
            for role, group in groups.items():
                if role in roles:
                    target.groups.add(group)
                elif role in current_groups:
                    target.groups.remove(group)

            if current_groups != roles:
                target.token_version += 1
                target.save(update_fields=["token_version"])
                recovery_invalidated_at = timezone.now()
                StaffSecurityChallenge.objects.filter(
                    user=target, consumed_at__isnull=True, invalidated_at__isnull=True
                ).update(invalidated_at=recovery_invalidated_at)
                StaffPasswordResetToken.objects.filter(
                    user=target, consumed_at__isnull=True
                ).update(consumed_at=recovery_invalidated_at)
            audit_request_action(
                request=request,
                action="SUPPORT_STAFF_ROLES_UPDATED",
                resource_type="CustomUser",
                resource_id=str(target.pk),
                metadata={
                    "before_roles": sorted(current_groups),
                    "after_roles": sorted(roles),
                },
            )

        return Response({
            "id": target.id,
            "email": target.email,
            "name": target.get_full_name() or target.email,
            "roles": user_support_roles(target),
        })