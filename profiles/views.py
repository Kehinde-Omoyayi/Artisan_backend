from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404


from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    OpenApiRequest,
    extend_schema,
    extend_schema_view,
)
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import (
    FormParser,
    JSONParser,
    MultiPartParser,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from home.models import *

from .serializers import *
from .discovery import (
    ArtisanDiscoveryDetailView,
    ArtisanDiscoveryListView,
    ArtisanProfessionalLinksReviewView,
)


def get_authenticated_customer(user):
    customer = getattr(user, "customer_profile", None)

    if customer is None and getattr(user, "artisan_profile", None) is not None:
        customer, _ = CustomerProfile.objects.get_or_create(user=user)

    if customer is None:
        raise PermissionDenied(
            "A customer profile is required for this endpoint."
        )

    return customer


def get_authenticated_artisan(user):
    artisan = getattr(user, "artisan_profile", None)

    if artisan is None:
        raise PermissionDenied(
            "An artisan profile is required for this endpoint."
        )

    return artisan



class ArtisanProfileView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        tags=["Artisan Profile"],
        operation_id="artisan_profile",
        summary="Retrieve artisan profile",
        request=None,
        description=(
            "Returns the authenticated artisan's complete profile, "
            "including personal details, contact information, profile photo, "
            "business information, experience, location, ratings, and status."
        ),
        responses={
            200: OpenApiResponse(
                response=ArtisanProfileSerializer,
                description="Artisan profile retrieved successfully.",
                examples=[
                    OpenApiExample(
                        "Artisan profile response",
                        value={
                            "id": "85aa860f-ab3e-4e00-ba1d-22ff0da6f1ee",
                            "first_name": "Kehinde",
                            "last_name": "Omoyayi",
                            "phone_number": "+2348012345678",
                            "email": "artisan@example.com",
                            "profile_photo": None,
                            "business_name": "Kehinde Plumbing Services",
                            "bio": "Professional plumbing and maintenance services.",
                            "years_experience": 5,
                            "country": "Nigeria",
                            "state": "Ogun",
                            "area": "Abeokuta",
                            "average_rating": "4.50",
                            "completion_rate": 95.0,
                            "cancellation_rate": 2.0,
                            "profile_status": "ACTIVE",
                        },
                        response_only=True,
                        status_codes=["200"],
                    ),
                ],
            ),
            401: OpenApiResponse(
                description="Authentication credentials are missing or invalid."
            ),
            403: OpenApiResponse(
                description="The authenticated user does not have an artisan profile."
            ),
        },
    )
    def get(self, request):
        artisan = get_authenticated_artisan(request.user)
        return Response(
            ArtisanProfileSerializer(
                artisan,
                context={"request": request},
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        tags=["Artisan Profile"],
        operation_id="artisan_profile_update",
        summary="Update artisan profile",
        description=(
            "Partially updates the authenticated artisan's profile. "
            "Only supplied fields are updated. Supports multipart/form-data "
            "for file uploads, application/json, and "
            "application/x-www-form-urlencoded. The profile is identified "
            "from the authenticated user."
        ),
        request={
            "multipart/form-data": OpenApiRequest(
                request=ArtisanProfileSerializer(partial=True),
                encoding={
                    "profile_photo": {
                        "contentType": "image/jpeg, image/png, image/webp",
                    },
                },
            ),
            "application/json": ArtisanProfileTextUpdateSerializer(partial=True),
            "application/x-www-form-urlencoded": ArtisanProfileTextUpdateSerializer(partial=True),
        },
        examples=[
            OpenApiExample(
                "Multipart profile update",
                summary="Update profile details and optionally upload a photo",
                description=(
                    "Select an image file in the profile_photo field "
                    "when uploading a new profile picture. Other fields "
                    "can be submitted without changing the photo."
                ),
                value={
                    "first_name": "Kehinde",
                    "last_name": "Omoyayi",
                    "phone_number": "+2348012345678",
                    "business_name": "Kehinde Plumbing Services",
                    "bio": "Professional plumbing and maintenance services.",
                    "years_experience": 5,
                    "country": "Nigeria",
                    "state": "Ogun",
                    "area": "Abeokuta",
                },
                request_only=True,
                media_type="multipart/form-data",
            ),
            OpenApiExample(
                "JSON profile update",
                summary="Update selected profile fields",
                value={
                    "bio": "Professional plumbing and maintenance services.",
                    "years_experience": 5,
                },
                request_only=True,
                media_type="application/json",
            ),
            OpenApiExample(
                "URL-encoded profile update",
                summary="Update business name",
                value={
                    "business_name": "Kehinde Plumbing Services",
                },
                request_only=True,
                media_type="application/x-www-form-urlencoded",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=ArtisanProfileSerializer,
                description="Artisan profile updated successfully.",
                examples=[
                    OpenApiExample(
                        "Updated artisan profile",
                        value={
                            "id": "85aa860f-ab3e-4e00-ba1d-22ff0da6f1ee",
                            "first_name": "Kehinde",
                            "last_name": "Omoyayi",
                            "phone_number": "+2348012345678",
                            "email": "artisan@example.com",
                            "profile_photo": None,
                            "business_name": "Kehinde Plumbing Services",
                            "bio": "Professional plumbing and maintenance services.",
                            "years_experience": 5,
                            "country": "Nigeria",
                            "state": "Ogun",
                            "area": "Abeokuta",
                            "average_rating": "4.50",
                            "completion_rate": 95.0,
                            "cancellation_rate": 2.0,
                            "profile_status": "ACTIVE",
                        },
                        response_only=True,
                        status_codes=["200"],
                    ),
                ],
            ),
            400: OpenApiResponse(
                description="One or more submitted fields failed validation.",
                examples=[
                    OpenApiExample(
                        "Validation error",
                        value={
                            "years_experience": [
                                "A valid integer is required."
                            ]
                        },
                        response_only=True,
                        status_codes=["400"],
                    ),
                ],
            ),
            401: OpenApiResponse(
                description="Authentication credentials are missing or invalid."
            ),
            403: OpenApiResponse(
                description="The authenticated user is not permitted to update an artisan profile."
            ),
            415: OpenApiResponse(
                description="The request content type is not supported."
            ),
        },
    )
    def patch(self, request):
        artisan = get_authenticated_artisan(request.user)
        serializer = ArtisanProfileSerializer(
            artisan,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            serializer.save()

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )
class CustomerProfileView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        tags=["Customer Profile"],
        operation_id="customer_profile",
        description=(
            "Returns the authenticated customer's profile, contact details, "
            "location, and cancellation risk information."
        ),
        responses={
            200: CustomerProfileSerializer,
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Customer profile not found."),
        },
    )
    def get(self, request):
        try:
            customer = get_authenticated_customer(request.user)
        except PermissionDenied:
            return Response(
                {"detail": "Customer profile not found."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(
            CustomerProfileSerializer(customer, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        tags=["Customer Profile"],
        operation_id="customer_profile_update",
        description=(
            "Partially updates the authenticated customer's profile "
            "and permitted user details."
        ),
        request=CustomerProfileSerializer(partial=True),
        responses={
            200: CustomerProfileSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Customer profile not found."),
        },
    )
    def patch(self, request):
        try:
            customer = get_authenticated_customer(request.user)
        except PermissionDenied:
            return Response(
                {"detail": "Customer profile not found."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CustomerProfileSerializer(
            customer,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )
