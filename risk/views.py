from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.audit import audit_request_action
from home.models import CustomerProfile
from support.models import SupportCase
from django.db.models import Q
from support.permissions import (
    SupportAgentPermission,
    SupportSupervisorPermission,
)

from .models import CustomerRiskProfile
from .serializers import (
    CustomerCancellationRiskSerializer,
    SupportCustomerRiskAdjustSerializer,
    SupportCustomerRiskSummarySerializer,
)
from .services import adjust_customer_risk_points, get_customer_risk_profile


class CustomerCancellationRiskView(APIView):
    """The customer's own risk status: score, percentage, history, adjustments."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Customer Risk"],
        summary="Get customer payment cancellation risk",
        description=(
            "Returns the customer's rolling payment cancellation risk score "
            "(points out of five and the matching percentage), the qualifying "
            "payment cancellations in the window and every manual adjustment "
            "made by customer support. Booking cancellations are never scored, "
            "and reaching the top of the scale raises a review: it never blocks "
            "the account."
        ),
        responses={
            200: CustomerCancellationRiskSerializer,
            403: OpenApiResponse(description="Only customers can view customer risk."),
        },
    )
    def get(self, request):
        customer = getattr(request.user, "customer_profile", None)
        if customer is None:
            raise PermissionDenied("Only customers can view customer risk.")
        profile = get_customer_risk_profile(customer)
        return Response(
            CustomerCancellationRiskSerializer(profile).data, status=status.HTTP_200_OK
        )


class SupportCustomerRiskPermission(SupportAgentPermission):
    """Agents, supervisors and support administrators may work risk appeals."""

    message = "Customer support access is required to manage risk points."


class SupportCustomerRiskSearchView(ListAPIView):
    """Search customers by name or email to investigate a risk appeal."""

    permission_classes = [IsAuthenticated, SupportCustomerRiskPermission]
    serializer_class = SupportCustomerRiskSummarySerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_risk"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return CustomerRiskProfile.objects.none()
        search = (self.request.query_params.get("search") or "").strip()
        queryset = CustomerRiskProfile.objects.select_related(
            "customer__user"
        ).order_by("-cancellation_points", "-updated_at")
        if search:
            queryset = queryset.filter(
                Q(customer__user__email__icontains=search)
                | Q(customer__user__first_name__icontains=search)
                | Q(customer__user__last_name__icontains=search)
            )
        return queryset[:25]

    @extend_schema(
        tags=["Customer Risk"],
        summary="Search customer risk profiles (support)",
        parameters=[
            OpenApiParameter(
                "search",
                str,
                OpenApiParameter.QUERY,
                description="Name or email fragment.",
            )
        ],
        responses={200: SupportCustomerRiskSummarySerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SupportCustomerRiskDetailView(APIView):
    """Full risk file for one customer: history, references and adjustments."""

    permission_classes = [IsAuthenticated, SupportCustomerRiskPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_risk"

    def _customer(self, customer_id):
        customer = (
            CustomerProfile.objects.select_related("user")
            .filter(pk=customer_id)
            .first()
        )
        if customer is None:
            raise NotFound("Customer not found.")
        return customer

    @extend_schema(
        tags=["Customer Risk"],
        summary="Read one customer's risk file (support)",
        responses={
            200: CustomerCancellationRiskSerializer,
            404: OpenApiResponse(description="Customer not found."),
        },
    )
    def get(self, request, customer_id):
        customer = self._customer(customer_id)
        profile = get_customer_risk_profile(customer)
        data = CustomerCancellationRiskSerializer(profile).data
        data["customer"] = {
            "id": str(customer.pk),
            "email": customer.user.email,
            "name": customer.user.get_full_name(),
            "is_active": customer.user.is_active,
            "security_status": customer.user.security_status,
        }
        data["event_points"] = profile.event_points
        data["manual_adjustment_points"] = profile.manual_adjustment_points
        return Response(data, status=status.HTTP_200_OK)


class SupportCustomerRiskAdjustView(APIView):
    """The single audited write surface for a risk point appeal."""

    permission_classes = [IsAuthenticated, SupportCustomerRiskPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_risk_adjust"

    @extend_schema(
        tags=["Customer Risk"],
        summary="Adjust a customer's risk points (support)",
        description=(
            "Add, remove, set or reset a customer's payment cancellation risk "
            "points. A reason is always required, the adjustment is written to "
            "the audit trail, the original payment cancellation history is never "
            "touched, and the customer is told the outcome. Customer support can "
            "never change any other customer field through this endpoint."
        ),
        request=SupportCustomerRiskAdjustSerializer,
        responses={
            200: CustomerCancellationRiskSerializer,
            400: OpenApiResponse(description="Invalid action, target or reason."),
            403: OpenApiResponse(description="Support access is required."),
            404: OpenApiResponse(description="Customer or support case not found."),
        },
        examples=[
            {
                "request": {
                    "action": "REMOVE",
                    "reason": "Payment cancelled after a provider outage; appeal upheld.",
                    "notify_customer": True,
                }
            }
        ],
    )
    def post(self, request, customer_id):
        from home.models import CustomerProfile

        customer = (
            CustomerProfile.objects.select_related("user")
            .filter(pk=customer_id)
            .first()
        )
        if customer is None:
            raise NotFound("Customer not found.")
        serializer = SupportCustomerRiskAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        case = None
        case_id = data.get("case_id")
        if case_id:
            case = SupportCase.objects.filter(pk=case_id).first()
            if case is None:
                raise NotFound("Support case not found.")

        try:
            profile, adjustment = adjust_customer_risk_points(
                customer=customer,
                actor=request.user,
                action=data["action"],
                reason=data["reason"],
                points=data.get("points"),
                support_case=case,
                notify_customer=data.get("notify_customer", True),
            )
        except ValueError as exc:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"reason": str(exc)}) from exc

        audit_request_action(
            request=request,
            action="CUSTOMER_RISK_ADJUSTED",
            resource_type="CustomerRiskProfile",
            resource_id=str(profile.pk),
            metadata={
                "customer_id": str(customer.pk),
                "action": adjustment.action,
                "previous_points": adjustment.previous_points,
                "new_points": adjustment.new_points,
                "delta": adjustment.delta,
                "case_reference": case.reference if case else "",
            },
        )
        payload = CustomerCancellationRiskSerializer(profile).data
        payload["adjustment"] = {
            "id": str(adjustment.id),
            "action": adjustment.action,
            "previous_points": adjustment.previous_points,
            "new_points": adjustment.new_points,
            "delta": adjustment.delta,
            "reason": adjustment.reason,
            "actor": request.user.email,
            "created_at": adjustment.created_at,
        }
        return Response(payload, status=status.HTTP_200_OK)
