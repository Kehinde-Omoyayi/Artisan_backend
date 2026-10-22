from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError

# Every artisan may publish at most three service offerings.
MAX_ARTISAN_SERVICES = 3
from rest_framework.parsers import (
    FormParser,
    JSONParser,
    MultiPartParser,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from home.authentication import MarketplaceJWTAuthentication
from home.permissions import IsPlatformSuperuser
from rest_framework.filters import OrderingFilter

from notifications.events import (
    notify_service_request_created,
    notify_service_request_updated,
    notify_service_request_withdrawn,
)
from home.models import *
from home.profile_completion import RequireCompleteCustomerProfile

from .kyc_crypto import (
    encrypt_bvn,
    encrypt_nin,
    encrypt_otp,
)
from .mono import MonoVerificationService
from .models import ArtisanVerification, ServiceRequest
from .serializers import *
from .tasks import (
    acquire_bvn_lock,
    delete_bvn_session,
    delete_bvn_state,
    get_bvn_session,
    get_bvn_state,
    initiate_bvn_async,
    request_bvn_otp_async,
    complete_bvn_async,
    verify_nin_async,
)
from .verification import (
    claim_verification,
    ensure_pending_bvn,
    get_artisan_verification,
)
from .verification_messages import (
    clear_verification_result,
    get_verification_result,
)


def get_authenticated_customer(user):
    customer = getattr(user, "customer_profile", None)

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


class ArtisanActivationView(APIView):
    """Allow staff to activate only eligible artisan profiles."""

    permission_classes = [IsPlatformSuperuser]

    @extend_schema(
        tags=["Artisan Administration"],
        operation_id="artisan_activation",
        description=(
            "Activate or deactivate an artisan profile. Activation requires "
            "an active user account, normal security status, and verified KYC."
        ),
        request=inline_serializer(
            name="ArtisanActivationRequest",
            fields={"activate": serializers.BooleanField()},
        ),
        responses={
            200: inline_serializer(
                name="ArtisanActivationResponse",
                fields={
                    "id": serializers.UUIDField(),
                    "profile_status": serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="The activate field must be a boolean."),
            403: OpenApiResponse(description="Staff permissions are required."),
            404: OpenApiResponse(description="Artisan profile was not found."),
            409: OpenApiResponse(description="The artisan is not eligible for activation."),
        },
    )
    def post(self, request, pk):
        activate = request.data.get("activate")
        if not isinstance(activate, bool):
            return Response(
                {"activate": ["A valid boolean is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        artisan = get_object_or_404(
            ArtisanProfile.objects.select_related("user"),
            pk=pk,
        )
        if activate:
            verification_status = (
                ArtisanVerification.objects
                .filter(artisan_id=artisan.pk)
                .values_list("verification_status", flat=True)
                .first()
            )
            eligible = (
                artisan.user.is_active
                and artisan.user.security_status == "NORMAL"
                and verification_status == "VERIFIED"
            )
            if not eligible:
                return Response(
                    {
                        "detail": (
                            "The artisan must have an active account, normal "
                            "security status, and verified profile before activation."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        target_status = "ACTIVE" if activate else "INACTIVE"
        ArtisanProfile.objects.filter(pk=artisan.pk).update(
            profile_status=target_status,
            updated_at=timezone.now(),
        )
        from profiles.discovery_cache import invalidate_public_discovery_cache
        transaction.on_commit(invalidate_public_discovery_cache, using=artisan._state.db)
        return Response(
            {"id": str(artisan.pk), "profile_status": target_status},
            status=status.HTTP_200_OK,
        )


class ArtisanVerificationStatusView(APIView):
    authentication_classes = [MarketplaceJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Artisan Verification"],
        operation_id="artisan_verification_status",
        description=(
            "Returns the verification state of the authenticated "
            "artisan. No artisan ID is accepted."
        ),
        responses={
            200: OpenApiResponse(
                response=VerificationStatusSerializer,
                description="Current verification status.",
            ),
            401: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Authentication required.",
            ),
        },
    )
    def get(self, request):
        artisan = get_authenticated_artisan(request.user)
        verification = get_artisan_verification(artisan)

        nin_result = get_verification_result(artisan.id)
        bvn_state = get_bvn_state(verification.id)

        message = None
        result_status = None

        if nin_result:
            result_status = nin_result.get("status")
            message = nin_result.get("message")

        if bvn_state and not message:
            result_status = bvn_state.get("status")
            message = bvn_state.get("message")

        if not message:
            messages = {
                "PENDING": (
                    "Your verification is currently being processed."
                ),
                "VERIFIED": (
                    "Your verification has been completed successfully."
                ),
                "REJECTED": (
                    "Your verification was not successful. "
                    "Please review the information provided and try again."
                ),
                "UNVERIFIED": (
                    "No verification request is currently active."
                ),
                "SUSPENDED": (
                    "Verification is suspended for this profile."
                ),
            }

            message = messages.get(
                verification.verification_status,
                "Verification status is currently unavailable.",
            )

        response_status = (
            result_status
            if result_status in {
                "SERVICE_ERROR",
                "RATE_LIMITED",
                "OTP_REQUIRED",
                "OTP_SENT",
                "OTP_REJECTED",
            }
            else verification.verification_status
        )

        return Response(
            {
                "status": response_status,
                "method": verification.verification_method,
                "tier": verification.verification_tier,
                "provider": verification.verification_provider,
                "reference": verification.verification_reference,
                "verified_at": verification.verified_at,
                "identity_name_available": bool(verification.verified_identity_name_encrypted),
                "message": message,
            },
            status=status.HTTP_200_OK,
        )


class NINVerificationView(APIView):
    authentication_classes = [MarketplaceJWTAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "verification_nin"

    @extend_schema(
        tags=["Artisan Verification"],
        operation_id="artisan_nin_verification",
        description=(
            "Submits the authenticated artisan's NIN for "
            "asynchronous verification through Mono. The NIN "
            "is encrypted and passed to Celery. It is not stored "
            "in the database."
        ),
        request=NINVerificationSerializer,
        responses={
            202: OpenApiResponse(
                description="NIN verification submitted.",
            ),
            400: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Invalid request.",
            ),
            401: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Authentication required.",
            ),
            403: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Verification is not allowed.",
            ),
            429: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Verification rate limit exceeded.",
            ),
        },
        examples=[
            OpenApiExample(
                "Request",
                value={
                    "nin": "00000000000",
                    "consent": True,
                },
                request_only=True,
            ),
            OpenApiExample(
                "Submitted",
                value={
                    "status": "PENDING",
                    "message": (
                        "Your NIN verification request has been "
                        "received and is now being processed. "
                        "Please check your verification status "
                        "after 1 minute."
                    ),
                    "verification_id": "UUID",
                    "next_step": (
                        "Check your verification status after 1 minute."
                    ),
                },
                response_only=True,
            ),
        ],
    )
    def post(self, request):
        artisan = get_authenticated_artisan(request.user)

        serializer = NINVerificationSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        verification = claim_verification(
            artisan,
            "NIN",
        )

        clear_verification_result(artisan.id)
        delete_bvn_state(verification.id)
        delete_bvn_session(verification.id)

        encrypted_nin = encrypt_nin(
            serializer.validated_data["nin"]
        )

        verify_nin_async.delay(
            str(verification.id),
            encrypted_nin,
        )

        return Response(
            {
                "status": "PENDING",
                "message": (
                    "Your NIN verification request has been "
                    "received and is now being processed. "
                    "Please check your verification status "
                    "after 1 minute."
                ),
                "verification_id": str(verification.id),
                "next_step": (
                    "Check your verification status after 1 minute."
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BVNInitiateView(APIView):
    authentication_classes = [MarketplaceJWTAuthentication]
    permission_classes = [IsAuthenticated]
    #throttle_classes = [ScopedRateThrottle]
    #throttle_scope = "verification_bvn_initiate"

    @extend_schema(
        tags=["Artisan Verification"],
        operation_id="artisan_bvn_verification_initiate",
        description=(
            "Submits the authenticated artisan's BVN for "
            "asynchronous verification through Mono."
        ),
        request=BVNVerificationSerializer,
        responses={
            202: OpenApiResponse(
                description="BVN verification submitted.",
            ),
            400: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Invalid request.",
            ),
            401: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Authentication required.",
            ),
            403: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Verification is not allowed.",
            ),
            429: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Rate limit exceeded.",
            ),
        },
        examples=[
            OpenApiExample(
                "Request",
                value={
                    "bvn": "00000000000",
                    "consent": True,
                },
                request_only=True,
            ),
            OpenApiExample(
                "Submitted",
                value={
                    "status": "PENDING",
                    "message": (
                        "Your BVN verification request has been "
                        "received and is now being processed. "
                        "Please check your verification status "
                        "after 1 minute."
                    ),
                    "verification_id": "UUID",
                    "next_step": (
                        "Check your verification status after 1 minute. or wait for our email"
                    ),
                },
                response_only=True,
            ),
        ],
    )
    def post(self, request):
        artisan = get_authenticated_artisan(request.user)

        serializer = BVNVerificationSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        verification = claim_verification(
            artisan,
            "BVN",
        )

        clear_verification_result(artisan.id)
        delete_bvn_state(verification.id)
        delete_bvn_session(verification.id)

        encrypted_bvn = encrypt_bvn(
            serializer.validated_data["bvn"]
        )

        initiate_bvn_async.delay(
            str(verification.id),
            encrypted_bvn,
        )

        return Response(
            {
                "status": "PENDING",
                "message": (
                    "Your BVN verification request has been "
                    "received and is now being processed. "
                    "Please check your verification status "
                    "after 1 minute."
                ),
                "verification_id": str(verification.id),
                "next_step": (
                    "Check your verification status after 1 minute."
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BVNMethodView(APIView):
    authentication_classes = [MarketplaceJWTAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "verification_bvn_method"

    @extend_schema(
        tags=["Artisan Verification"],
        operation_id="artisan_bvn_request_email_otp",
        description=(
            "Requests the BVN verification code through email "
            "after asynchronous BVN initiation has completed."
        ),
        request=None,
        responses={
            202: OpenApiResponse(
                description="OTP request queued.",
            ),
            400: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="BVN session is unavailable.",
            ),
            401: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Authentication required.",
            ),
            403: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Verification is not allowed.",
            ),
            429: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="OTP request rate limit exceeded.",
            ),
        },
    )
    def post(self, request):
        artisan = get_authenticated_artisan(request.user)
        verification = ensure_pending_bvn(artisan)

        state = get_bvn_state(verification.id)

        if state.get("status") != "OTP_REQUIRED":
            return Response(
                {
                    "status": state.get(
                        "status",
                        "PENDING",
                    ),
                    "message": state.get(
                        "message",
                        (
                            "Your BVN verification is still "
                            "being processed."
                        ),
                    ),
                    "next_step": state.get(
                        "next_step",
                        "CHECK_VERIFICATION_STATUS",
                    ),
                    "next_endpoint": state.get(
                        "next_endpoint",
                        "/api/v1/services/verification/",
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not get_bvn_session(verification.id):
            return Response(
                {
                    "status": "SESSION_EXPIRED",
                    "message": (
                        "Your BVN verification session has expired. "
                        "Please start BVN verification again."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not acquire_bvn_lock(
            verification.id,
            "method",
        ):
            return Response(
                {
                    "status": "PROCESSING",
                    "message": (
                        "Your BVN verification code request "
                        "is already being processed."
                    ),
                },
                status=status.HTTP_202_ACCEPTED,
            )

        request_bvn_otp_async.delay(
            str(verification.id)
        )

        return Response(
            {
                "status": "PENDING",
                "message": (
                    "Your BVN verification code request "
                    "has been submitted."
                ),
                "next_step": (
                    "Check your verification status after 1 minute."
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BVNDetailsView(APIView):
    authentication_classes = [MarketplaceJWTAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "verification_bvn_details"

    @extend_schema(
        tags=["Artisan Verification"],
        operation_id="artisan_bvn_verification_complete",
        description=(
            "Submits the six-digit BVN email OTP for "
            "asynchronous verification."
        ),
        request=OTPSerializer,
        responses={
            202: OpenApiResponse(
                description="OTP submitted.",
            ),
            400: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Invalid or unavailable OTP flow.",
            ),
            401: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Authentication required.",
            ),
            403: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="Verification is not allowed.",
            ),
            429: OpenApiResponse(
                response=VerificationErrorResponseSerializer,
                description="OTP rate limit exceeded.",
            ),
        },
        examples=[
            OpenApiExample(
                "Request",
                value={"otp": "123456"},
                request_only=True,
            ),
            OpenApiExample(
                "Submitted",
                value={
                    "status": "PENDING",
                    "message": (
                        "Your BVN verification code has been "
                        "submitted and is being processed."
                    ),
                    "next_step": (
                        "Check your verification status after 1 minute."
                    ),
                },
                response_only=True,
            ),
        ],
    )
    def post(self, request):
        artisan = get_authenticated_artisan(request.user)
        verification = ensure_pending_bvn(artisan)

        serializer = OTPSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        session_id = get_bvn_session(
            verification.id
        )

        if not session_id:
            return Response(
                {
                    "status": "SESSION_EXPIRED",
                    "message": (
                        "Your BVN verification session has expired. "
                        "Please start BVN verification again."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not acquire_bvn_lock(
            verification.id,
            "details",
        ):
            return Response(
                {
                    "status": "PROCESSING",
                    "message": (
                        "Your BVN verification code is "
                        "already being processed."
                    ),
                },
                status=status.HTTP_202_ACCEPTED,
            )

        encrypted_otp = encrypt_otp(
            serializer.validated_data["otp"]
        )

        complete_bvn_async.delay(
            str(verification.id),
            encrypted_otp,
        )

        return Response(
            {
                "status": "PENDING",
                "message": (
                    "Your BVN verification code has been "
                    "submitted and is being processed."
                ),
                "next_step": (
                    "Check your verification status after 1 minute."
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ServiceRequestListCreateView(generics.ListCreateAPIView):
    serializer_class = ServiceRequestSerializer
    permission_classes = [IsAuthenticated, RequireCompleteCustomerProfile]

    ordering_fields = [
        "created_at",
        "updated_at",
        "preferred_date",
        "urgency",
    ]
    ordering = ["-updated_at"]

    @extend_schema(
        tags=["Service Requests"],
        operation_id="service_request_list",
        summary="List my service requests",
        description=(
            "Lists requests belonging to the authenticated customer. "
            "Filter by service_name, category_name, location fields or urgency. "
            "Service and location IDs are not required as input."
        ),
        parameters=[
            OpenApiParameter(
                name="service_name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by exact service name.",
            ),
            OpenApiParameter(
                name="category_name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by exact category name.",
            ),
            OpenApiParameter(
                name="location_country",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by country.",
            ),
            OpenApiParameter(
                name="location_state",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by state.",
            ),
            OpenApiParameter(
                name="location_area",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by area.",
            ),
            OpenApiParameter(
                name="urgency",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by urgency.",
            ),
            OpenApiParameter(
                name="ordering",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description=(
                    "Supported fields: created_at, updated_at, "
                    "preferred_date, urgency. Prefix with '-' for descending."
                ),
            ),
        ],
        responses={
            200: ServiceRequestSerializer(many=True),
            401: OpenApiResponse(
                description="Authentication credentials are missing or invalid."
            ),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["Service Requests"],
        operation_id="service_request_create",
        summary="Request a service",
        description=(
            "Creates a service request for the authenticated customer. "
            "The backend resolves the selected service by name, determines "
            "its category and resolves the submitted country, state and area "
            "to an existing location. The service-request notification "
            "workflow is triggered after the database transaction commits."
        ),
        request=ServiceRequestSerializer,
        responses={
            201: OpenApiResponse(
                response=ServiceRequestSerializer,
                description="Service request created successfully.",
            ),
            400: OpenApiResponse(
                description="Invalid service, location or request details."
            ),
            401: OpenApiResponse(
                description="Authentication credentials are missing or invalid."
            ),
            403: OpenApiResponse(
                description="A customer profile is required."
            ),
        },
        examples=[
            OpenApiExample(
                "Request a service",
                value={
                    "service_name": "Electrical Installation",
                    "location": {
                        "country": "Nigeria",
                        "state": "Ogun",
                        "area": "Abeokuta",
                    },
                    "preferred_date": "2026-10-15",
                    "urgency": "MODERATE",
                    "budget": "10000.00",
                    "description": "I need an electrician for a repair.",
                },
                request_only=True,
                media_type="application/json",
            ),
        ],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ServiceRequest.objects.none()
        queryset = (
            ServiceRequest.objects
            .filter(requester=self.request.user)
            .select_related(
                "requester",
                "service",
                "category",
                "location",
            )
        )

        params = self.request.query_params

        # Attended and archived requests stay in the database but leave every
        # customer-facing list. `status=all` (or a specific status) exposes the
        # history for reporting and support.
        requested_status = str(params.get("status", "") or "").strip().upper()
        if requested_status in {"", "OPEN"}:
            queryset = queryset.filter(status=ServiceRequest.Status.OPEN)
        elif requested_status != "ALL":
            queryset = queryset.filter(status=requested_status)

        service_name = params.get("service_name")
        category_name = params.get("category_name")
        country = params.get("location_country")
        state = params.get("location_state")
        area = params.get("location_area")
        urgency = params.get("urgency")

        if service_name:
            queryset = queryset.filter(
                service__name__iexact=service_name.strip()
            )

        if category_name:
            queryset = queryset.filter(
                category__name__iexact=category_name.strip()
            )

        if country:
            queryset = queryset.filter(
                location__country__iexact=country.strip()
            )

        if state:
            queryset = queryset.filter(
                location__state__iexact=state.strip()
            )

        if area:
            queryset = queryset.filter(
                location__area__icontains=area.strip()
            )

        if urgency:
            queryset = queryset.filter(urgency=urgency.strip())

        ordering = self.request.query_params.get(
            "ordering",
            "-updated_at",
        ).strip()

        allowed_ordering = {
            "created_at",
            "updated_at",
            "preferred_date",
            "urgency",
        }

        ordering_field = ordering.lstrip("-")

        if ordering.startswith("--") or ordering_field not in allowed_ordering:
            ordering = "-updated_at"

        return queryset.order_by(ordering)

    def perform_create(self, serializer):
        get_authenticated_customer(self.request.user)

        with transaction.atomic():
            service_request = serializer.save(
                requester=self.request.user
            )

            notify_service_request_created(service_request)


class ArtisanMatchedServiceRequestListView(generics.ListAPIView):
    """Read-only view of requests matching this artisan's active service area."""

    serializer_class = ArtisanMatchedServiceRequestSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Service Requests"],
        operation_id="artisan_matched_service_requests",
        summary="List requests matching my services",
        description=(
            "Returns requests matching the authenticated artisan's active services, "
            "service area, active profile and verified status. Requester IDs and "
            "contact details are not included."
        ),
        responses={
            200: ArtisanMatchedServiceRequestSerializer(many=True),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="An artisan profile is required."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ServiceRequest.objects.none()
        artisan = get_authenticated_artisan(self.request.user)
        from .request_matching import matched_requests_for
        queryset = matched_requests_for(artisan)
        from .request_filters import filter_matched_requests
        return filter_matched_requests(queryset, self.request.query_params)


class ArtisanRequestDismissView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=['Service Requests'], summary='Hide a matched request for this artisan only', request=None, responses={200: OpenApiResponse(description='Personal not-interested choice saved; customer request remains open.')})
    def post(self, request, pk):
        from .models import ArtisanRequestDismissal
        from .request_matching import matched_requests_for
        from notifications.events import notify_request_not_interested
        artisan = get_authenticated_artisan(request.user)
        with transaction.atomic():
            item = get_object_or_404(matched_requests_for(artisan, include_dismissed=True).select_for_update(of=('self',)), pk=pk)
            dismissal, created = ArtisanRequestDismissal.objects.get_or_create(request=item, artisan=artisan)
            if created:
                notify_request_not_interested(item, artisan)
        return Response({'request_id': item.pk, 'hidden': True, 'detail': 'Hidden from your matches only. The request remains available to other eligible artisans.'}, status=status.HTTP_200_OK)


@extend_schema_view(
    get=extend_schema(
        tags=["Service Requests"],
        operation_id="service_request_detail",
        summary="Retrieve a service request",
        description=(
            "Retrieves a service request belonging to the authenticated customer."
        ),
        responses={
            200: ServiceRequestSerializer,
            401: OpenApiResponse(description="Authentication required."),
            404: OpenApiResponse(
                description="Request not found or not owned by this customer."
            ),
        },
    ),
    put=extend_schema(
        tags=["Service Requests"],
        operation_id="service_request_replace",
        summary="Replace a service request",
        request=ServiceRequestSerializer,
        responses={
            200: ServiceRequestSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            404: OpenApiResponse(description="Service request not found."),
        },
    ),
    patch=extend_schema(
        tags=["Service Requests"],
        operation_id="service_request_update",
        summary="Update a service request",
        description=(
            "Updates submitted fields on a request belonging to the "
            "authenticated customer. Use service_name to select a service "
            "and location to select a country, state and area. Every matching "
            "artisan in the request's category and area is notified of the change."
        ),
        request=ServiceRequestSerializer(partial=True),
        responses={
            200: ServiceRequestSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            404: OpenApiResponse(description="Service request not found."),
        },
    ),
    delete=extend_schema(
        tags=["Service Requests"],
        operation_id="service_request_withdraw",
        summary="Withdraw a service request",
        description=(
            "Soft-deletes the request. The row is marked ARCHIVED, disappears from "
            "every customer-facing list, and is kept on record for reference and "
            "future analysis. Matching artisans in the same category and area are "
            "told that the request is no longer available."
        ),
        responses={
            200: OpenApiResponse(description="Service request archived."),
            401: OpenApiResponse(description="Authentication required."),
            404: OpenApiResponse(description="Service request not found."),
        },
    ),
)
class ServiceRequestDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Read, edit or withdraw one of the customer's own requests.

    Delete is a soft delete: the row becomes ARCHIVED and leaves the frontend, while
    bookings, conversations and reports keep their reference to it.
    """

    serializer_class = ServiceRequestSerializer
    permission_classes = [IsAuthenticated, RequireCompleteCustomerProfile]

    def get_queryset(self):
        return (
            ServiceRequest.objects
            .filter(requester=self.request.user)
            .select_related(
                "requester",
                "service",
                "category",
                "location",
            )
        )

    def perform_update(self, serializer):
        current = self.get_object()
        if current.status != ServiceRequest.Status.OPEN:
            raise ValidationError(
                {
                    "detail": (
                        "This request has been attended to or withdrawn and can no "
                        "longer be edited."
                    )
                }
            )
        with transaction.atomic():
            service_request = serializer.save()
            notify_service_request_updated(service_request)

    def destroy(self, request, *args, **kwargs):
        service_request = self.get_object()
        with transaction.atomic():
            changed = service_request.archive()
            if changed:
                notify_service_request_withdrawn(service_request)
        return Response(
            {
                "id": service_request.id,
                "status": service_request.status,
                "archived_at": service_request.archived_at,
                "detail": "Service request withdrawn.",
            },
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Locations"],
        operation_id="location_list",
        summary="List available locations",
        description=(
            "Returns existing locations for frontend dropdowns. "
            "Filter by country, state and area."
        ),
        parameters=[
            OpenApiParameter(
                name="country",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by country.",
            ),
            OpenApiParameter(
                name="state",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by state.",
            ),
            OpenApiParameter(
                name="area",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by area using partial matching.",
            ),
        ],
        responses={
            200: LocationSerializer(many=True),
            401: OpenApiResponse(description="Authentication required."),
        },
    ),
    post=extend_schema(
        tags=["Locations"],
        operation_id="location_create",
        summary="Create a location",
        description="Creates a location catalogue record. Administrator access is required.",
        request=LocationSerializer,
        responses={
            201: LocationSerializer,
            400: OpenApiResponse(description="Invalid location."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Administrator access required."),
        },
        examples=[
            OpenApiExample(
                "Create location",
                value={
                    "country": "Nigeria",
                    "state": "Ogun",
                    "area": "Abeokuta",
                },
                request_only=True,
            ),
        ],
    ),
)


class LocationListCreateView(generics.ListCreateAPIView):
    serializer_class = LocationSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsPlatformSuperuser()]
        return [AllowAny()]

    def get_queryset(self):
        queryset = Locations.objects.all().order_by(
            "country",
            "state",
            "area",
        )

        country = self.request.query_params.get("country")
        state = self.request.query_params.get("state")
        area = self.request.query_params.get("area")

        if country:
            queryset = queryset.filter(
                country__iexact=country.strip()
            )

        if state:
            queryset = queryset.filter(
                state__iexact=state.strip()
            )

        if area:
            queryset = queryset.filter(
                area__icontains=area.strip()
            )

        return queryset


@extend_schema_view(
    get=extend_schema(
        tags=["Locations"],
        operation_id="location_detail",
        summary="Retrieve a location",
        responses={
            200: LocationSerializer,
            401: OpenApiResponse(description="Authentication required."),
            404: OpenApiResponse(description="Location not found."),
        },
    ),
    put=extend_schema(
        tags=["Locations"],
        operation_id="location_replace",
        summary="Replace a location",
        request=LocationSerializer,
        responses={
            200: LocationSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Administrator access required."),
            404: OpenApiResponse(description="Location not found."),
        },
    ),
    patch=extend_schema(
        tags=["Locations"],
        operation_id="location_update",
        summary="Update a location",
        request=LocationSerializer(partial=True),
        responses={
            200: LocationSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Administrator access required."),
            404: OpenApiResponse(description="Location not found."),
        },
    ),
    delete=extend_schema(
        tags=["Locations"],
        operation_id="location_delete",
        summary="Delete a location",
        responses={
            204: OpenApiResponse(description="Location deleted successfully."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Administrator access required."),
            404: OpenApiResponse(description="Location not found."),
        },
    ),
)
class LocationDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = LocationSerializer
    queryset = Locations.objects.all()

    def get_permissions(self):
        if self.request.method in {"PUT", "PATCH", "DELETE"}:
            return [IsPlatformSuperuser()]
        return [IsAuthenticated()]


@extend_schema_view(
    get=extend_schema(
        tags=["Institutions"],
        operation_id="institution_list",
        summary="List active institutions for discovery filters",
        description=(
            "Returns admin-managed institutions with stable database IDs and "
            "their existing marketplace location. No institution or location "
            "values are fabricated by the discovery frontend."
        ),
        parameters=[
            OpenApiParameter(name="q", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="country", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="state", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
        ],
        responses={200: InstitutionSerializer(many=True)},
    ),
)
class InstitutionListView(generics.ListAPIView):
    serializer_class = InstitutionSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def list(self, request, *args, **kwargs):
        if 'limit' not in request.query_params:
            return super().list(request, *args, **kwargs)
        from rest_framework.pagination import LimitOffsetPagination
        paginator = LimitOffsetPagination()
        paginator.default_limit = 50
        paginator.max_limit = 100
        rows = paginator.paginate_queryset(self.get_queryset(), request, view=self)
        return paginator.get_paginated_response(self.get_serializer(rows, many=True).data)

    def get_queryset(self):
        queryset = Institution.objects.filter(is_active=True).select_related("location")
        params = self.request.query_params
        search = " ".join(str(params.get("q") or "").split())[:160]
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(campus__icontains=search)
                | Q(abbreviation__icontains=search)
                | Q(abbreviations__icontains=search)
                | Q(aliases__icontains=search)
                | Q(location__area__icontains=search)
                | Q(location__state__icontains=search)
                | Q(location__country__icontains=search)
            )
        country = str(params.get("country") or "").strip()
        state = str(params.get("state") or "").strip()
        area = str(params.get("area") or "").strip()
        identifier = str(params.get("id") or "").strip()
        if identifier:
            from uuid import UUID
            try:
                identifier = UUID(identifier)
            except ValueError:
                raise ValidationError({'id': 'Choose a valid institution.'})
            queryset = queryset.filter(pk=identifier)
        if country:
            queryset = queryset.filter(location__country__iexact=country)
        if state:
            queryset = queryset.filter(location__state__iexact=state)
        if area:
            queryset = queryset.filter(location__area__iexact=area)
        # Alphabetical with Nigeria first when no explicit country filter
        from django.db.models import Case, When, IntegerField
        if not country:
            return queryset.order_by(
                Case(When(location__country__iexact="Nigeria", then=0), default=1, output_field=IntegerField()),
                "name",
                "campus",
                "pk",
            )
        return queryset.order_by("name", "campus", "pk")


@extend_schema_view(
    get=extend_schema(
        tags=["Service Categories"],
        operation_id="service_category_list",
        summary="List service categories",
        description="Publicly returns categories that contain at least one active service.",
        parameters=[
            OpenApiParameter(
                name="name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by category name.",
            ),
        ],
        responses={
            200: ServiceCategorySerializer(many=True),
        },
    ),
)
class ServiceCategoryListView(generics.ListAPIView):
    serializer_class = ServiceCategorySerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        queryset = (
            ServiceCategory.objects
            .filter(services__is_active=True)
            .distinct()
        )

        name = self.request.query_params.get("name")

        if name:
            queryset = queryset.filter(name__iexact=name.strip())

        return queryset.order_by("name")


@extend_schema_view(
    get=extend_schema(
        tags=["Services"],
        operation_id="service_list",
        summary="List available services",
        description=(
            "Publicly returns active services with database-generated IDs, names, "
            "descriptions and category names. Filter by category_name. "
            "The ID is read-only; service selection in write requests uses "
            "service_name."
        ),
        parameters=[
            OpenApiParameter(
                name="category_name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by category name.",
            ),
        ],
        responses={
            200: ServiceSerializer(many=True),
        },
    ),
)
class ServiceListView(generics.ListAPIView):
    serializer_class = ServiceSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        queryset = (
            Service.objects
            .select_related("category")
            .filter(is_active=True)
        )

        category_name = self.request.query_params.get("category_name")

        if category_name:
            queryset = queryset.filter(
                category__name__iexact=category_name.strip()
            )

        return queryset.order_by("name")


@extend_schema_view(
    get=extend_schema(
        tags=["Artisan Services"],
        operation_id="artisan_service_list",
        summary="List my service offerings",
        description=(
            "Lists offerings belonging to the authenticated artisan. "
            "Use service_name and location fields as filters."
        ),
        parameters=[
            OpenApiParameter(
                name="service_name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by exact service name.",
            ),
            OpenApiParameter(
                name="location_country",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="location_state",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="location_area",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
        ],
        responses={
            200: ArtisanServiceSerializer(many=True),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Artisan profile required."),
        },
    ),
    post=extend_schema(
        tags=["Artisan Services"],
        operation_id="artisan_service_create",
        summary="Add a service offering",
        description=(
            "Adds an existing service from the catalogue to the "
            "authenticated artisan's offerings. Submit service_name, "
            "not service_id. Set location to null for a general offering."
        ),
        request=ArtisanServiceSerializer,
        responses={
            201: ArtisanServiceSerializer,
            400: OpenApiResponse(
                description="Invalid service, location or starting price."
            ),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(
                description="The artisan cannot manage offerings."
            ),
        },
        examples=[
            OpenApiExample(
                "Offering in a specific location",
                value={
                    "service_name": "Electrical Installation",
                    "location": {
                        "country": "Nigeria",
                        "state": "Ogun",
                        "area": "Abeokuta",
                    },
                    "starting_price": "10000.00",
                },
                request_only=True,
            ),
            OpenApiExample(
                "General offering",
                value={
                    "service_name": "Electrical Installation",
                    "location": None,
                    "starting_price": "10000.00",
                },
                request_only=True,
            ),
        ],
    ),
)
class ArtisanServiceListCreateView(generics.ListCreateAPIView):
    serializer_class = ArtisanServiceSerializer
    permission_classes = [IsAuthenticated]

    def get_artisan(self):
        return get_authenticated_artisan(self.request.user)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ArtisanService.objects.none()
        queryset = (
            ArtisanService.objects
            .filter(artisan=self.get_artisan())
            .select_related("artisan", "service", "location")
        )

        params = self.request.query_params

        service_name = params.get("service_name")
        country = params.get("location_country")
        state = params.get("location_state")
        area = params.get("location_area")

        if service_name:
            queryset = queryset.filter(
                service__name__iexact=service_name.strip()
            )

        if country:
            queryset = queryset.filter(
                location__country__iexact=country.strip()
            )

        if state:
            queryset = queryset.filter(
                location__state__iexact=state.strip()
            )

        if area:
            queryset = queryset.filter(
                location__area__icontains=area.strip()
            )

        return queryset.order_by("service__name")

    def perform_create(self, serializer):
        artisan = self.get_artisan()

        if (
            artisan.profile_status in {"SUSPENDED", "INACTIVE"}
            or not artisan.user.is_active
            or getattr(artisan.user, "security_status", "NORMAL") != "NORMAL"
        ):
            raise PermissionDenied(
                "Your artisan profile cannot manage service offerings "
                "while inactive, suspended or restricted."
            )

        # Every artisan may publish at most three service offerings.
        existing = ArtisanService.objects.filter(artisan=artisan).count()
        if existing >= MAX_ARTISAN_SERVICES:
            raise ValidationError({
                "detail": (
                    f"You can publish at most {MAX_ARTISAN_SERVICES} services. "
                    "Remove one of your current offerings before adding another."
                )
            })

        serializer.save(artisan=artisan)


@extend_schema_view(
    get=extend_schema(
        tags=["Artisan Services"],
        operation_id="artisan_service_detail",
        summary="Retrieve one of my service offerings",
        responses={
            200: ArtisanServiceSerializer,
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Artisan profile required."),
            404: OpenApiResponse(description="Offering not found."),
        },
    ),
    put=extend_schema(
        tags=["Artisan Services"],
        operation_id="artisan_service_replace",
        summary="Replace a service offering",
        request=ArtisanServiceSerializer,
        responses={
            200: ArtisanServiceSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Cannot manage offerings."),
            404: OpenApiResponse(description="Offering not found."),
        },
    ),
    patch=extend_schema(
        tags=["Artisan Services"],
        operation_id="artisan_service_update",
        summary="Update a service offering",
        request=ArtisanServiceSerializer(partial=True),
        responses={
            200: ArtisanServiceSerializer,
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Cannot manage offerings."),
            404: OpenApiResponse(description="Offering not found."),
        },
    ),
    delete=extend_schema(
        tags=["Artisan Services"],
        operation_id="artisan_service_delete",
        summary="Delete a service offering",
        responses={
            204: OpenApiResponse(description="Offering deleted successfully."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="Cannot manage offerings."),
            404: OpenApiResponse(description="Offering not found."),
        },
    ),
)
class ArtisanServiceDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ArtisanServiceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        artisan = get_authenticated_artisan(self.request.user)

        return (
            ArtisanService.objects
            .filter(artisan=artisan)
            .select_related("artisan", "service", "location")
        )

    def _assert_manageable(self):
        artisan = get_authenticated_artisan(self.request.user)

        if (
            artisan.profile_status in {"SUSPENDED", "INACTIVE"}
            or not artisan.user.is_active
            or getattr(artisan.user, "security_status", "NORMAL") != "NORMAL"
        ):
            raise PermissionDenied(
                "Your artisan profile cannot manage service offerings "
                "while inactive, suspended or restricted."
            )

        return artisan

    def perform_update(self, serializer):
        artisan = self._assert_manageable()
        serializer.save(artisan=artisan)

    def perform_destroy(self, instance):
        self._assert_manageable()
        instance.delete()