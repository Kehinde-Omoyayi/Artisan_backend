from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from .serializers import ReviewSerializer
from core.idempotency import idempotent_action

class ReviewCreateView(generics.CreateAPIView):
    serializer_class = ReviewSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        description="Allows a customer to review an artisan after a completed booking.",
        request=ReviewSerializer,
        responses={
            201: ReviewSerializer,
            400: OpenApiResponse(description="Invalid booking, rating, or review."),
            401: OpenApiResponse(description="Authentication required."),
        },
        examples=[
            OpenApiExample(
                "Submit review",
                request_only=True,
                value={
                    "booking": 42,
                    "rating": 5,
                    "comment": "Excellent work and very professional."
                },
            )
        ],
    )
    @idempotent_action("review.create", resource_type="Review")
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)
