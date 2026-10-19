from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import CustomerCancellationRiskSerializer
from .services import get_customer_risk_profile


class CustomerCancellationRiskView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Customer Risk"],
        summary="Get customer cancellation risk",
        description=(
            "Returns the customer's rolling cancellation risk score and weighted cancellation rate. "
            "The score is capped at 5 and an automated suspension occurs at 5/5."
        ),
        responses={200: CustomerCancellationRiskSerializer, 403: OpenApiResponse(description="Only customers can view customer cancellation risk.")},
    )
    def get(self, request):
        customer = getattr(request.user, "customer_profile", None)
        if customer is None:
            raise PermissionDenied("Only customers can view customer cancellation risk.")
        profile = get_customer_risk_profile(customer)
        return Response(CustomerCancellationRiskSerializer(profile).data, status=status.HTTP_200_OK)
