from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db import models
from django.db.models import Prefetch
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from home.models import ArtisanService
from support.permissions import user_has_support_role

from .eligibility import promotion_eligibility
from .models import (
    PromotionApplication,
    PromotionApplicationDecision,
    PromotionApplicationEvidence,
    PromotionApplicationRevision,
)
from .permissions import (
    CanReviewPromotionApplications,
    IsArtisanApplicant,
    PROMOTION_REVIEW_ROLES,
)
from .serializers import (
    PromotionApplicationApplicantSerializer,
    PromotionApplicationQueueSerializer,
    PromotionApplicationReviewDetailSerializer,
    PromotionApplicationSubmissionSerializer,
    PromotionDecisionSerializer,
)
from .services import (
    create_promotion_application,
    decide_promotion_application,
    resubmit_promotion_application,
    withdraw_promotion_application,
)


class PromotionApplicationPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 60


def _application_queryset(queryset):
    revision_queryset = PromotionApplicationRevision.objects.prefetch_related(
        Prefetch(
            "evidence",
            queryset=PromotionApplicationEvidence.objects.order_by("uploaded_at", "id"),
            to_attr="prefetched_evidence",
        )
    ).order_by("revision_number")
    decision_queryset = PromotionApplicationDecision.objects.select_related(
        "actor", "revision"
    ).order_by("-created_at", "-id")
    return (
        queryset.select_related(
            "artisan__user",
            "artisan__verification",
            "institution__location",
            "area_location",
            "last_reviewed_by",
        )
        .prefetch_related(
            Prefetch("revisions", queryset=revision_queryset, to_attr="prefetched_revisions"),
            Prefetch("decisions", queryset=decision_queryset, to_attr="prefetched_decisions"),
            Prefetch(
                "services",
                queryset=ArtisanService.objects.select_related("service__category", "location").order_by("service__name", "pk"),
                to_attr="prefetched_services",
            ),
        )
    )


class PromotionEligibilityView(APIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]

    @extend_schema(
        tags=["Promotion Applications"],
        summary="Check current artisan eligibility to apply for a promotion",
        responses={200: OpenApiTypes.OBJECT, 403: OpenApiResponse(description="An artisan profile is required.")},
    )
    def get(self, request):
        return Response(promotion_eligibility(request.user.artisan_profile))


class PromotionApplicationListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = PromotionApplicationApplicantSerializer
    pagination_class = PromotionApplicationPagination

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PromotionApplication.objects.none()
        return _application_queryset(
            PromotionApplication.objects.filter(artisan=self.request.user.artisan_profile)
        )

    @extend_schema(
        tags=["Promotion Applications"],
        summary="List my applications or submit a new target-specific application",
        request=PromotionApplicationSubmissionSerializer,
        responses={
            200: PromotionApplicationApplicantSerializer(many=True),
            201: PromotionApplicationApplicantSerializer,
            400: OpenApiResponse(description="Eligibility or application validation failed."),
            401: OpenApiResponse(description="Authentication required."),
            403: OpenApiResponse(description="An active artisan account is required."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = PromotionApplicationSubmissionSerializer(
            data=request.data,
            context={"request": request, "artisan": request.user.artisan_profile},
        )
        serializer.is_valid(raise_exception=True)
        try:
            application = create_promotion_application(
                artisan=request.user.artisan_profile,
                validated_data=serializer.validated_data,
            )
        except IntegrityError:
            raise ValidationError({
                "promotion_type": "An application for this exact promotion type and target is already awaiting review, changes or purchase.",
            }) from None
        response_serializer = PromotionApplicationApplicantSerializer(
            _application_queryset(PromotionApplication.objects.filter(pk=application.pk)).get(),
            context={"request": request},
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class PromotionApplicationDetailView(APIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_application(self, request, pk):
        return get_object_or_404(
            _application_queryset(PromotionApplication.objects.filter(artisan=request.user.artisan_profile)),
            pk=pk,
        )

    @extend_schema(
        tags=["Promotion Applications"],
        summary="Retrieve my own promotion application",
        request=None,
        responses={200: PromotionApplicationApplicantSerializer, 400: OpenApiResponse(description="Validation failed."), 404: OpenApiResponse(description="Application not found."), 409: OpenApiResponse(description="Application is not awaiting corrections.")},
    )
    def get(self, request, pk):
        application = self.get_application(request, pk)
        return Response(PromotionApplicationApplicantSerializer(application, context={"request": request}).data)

    @extend_schema(
        tags=["Promotion Applications"],
        summary="Edit and resubmit an application marked Changes Required",
        request=PromotionApplicationSubmissionSerializer,
        responses={
            200: PromotionApplicationApplicantSerializer,
            400: OpenApiResponse(description="Validation failed."),
            404: OpenApiResponse(description="Application not found."),
            409: OpenApiResponse(description="Application is not awaiting corrections."),
        },
    )
    def patch(self, request, pk):
        application = self.get_application(request, pk)
        if application.status != PromotionApplication.Status.CHANGES_REQUIRED:
            raise ValidationError({"status": "Only an application marked Changes Required can be edited and resubmitted."})
        serializer = PromotionApplicationSubmissionSerializer(
            data=request.data,
            context={"request": request, "artisan": request.user.artisan_profile},
        )
        serializer.is_valid(raise_exception=True)
        try:
            updated = resubmit_promotion_application(
                application=application,
                artisan=request.user.artisan_profile,
                validated_data=serializer.validated_data,
            )
        except IntegrityError:
            raise ValidationError({
                "promotion_type": "An application for this exact promotion type and target is already awaiting review, changes or purchase.",
            }) from None
        fresh = _application_queryset(PromotionApplication.objects.filter(pk=updated.pk)).get()
        return Response(PromotionApplicationApplicantSerializer(fresh, context={"request": request}).data)


class PromotionApplicationWithdrawView(APIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]

    @extend_schema(
        tags=["Promotion Applications"],
        request=None,
        responses={200: PromotionApplicationApplicantSerializer, 400: OpenApiResponse(description="Application cannot be withdrawn."), 404: OpenApiResponse(description="Application not found.")},
    )
    def post(self, request, pk):
        application = get_object_or_404(
            PromotionApplication.objects.filter(artisan=request.user.artisan_profile),
            pk=pk,
        )
        updated = withdraw_promotion_application(
            application=application,
            artisan=request.user.artisan_profile,
        )
        fresh = _application_queryset(PromotionApplication.objects.filter(pk=updated.pk)).get()
        return Response(PromotionApplicationApplicantSerializer(fresh, context={"request": request}).data)


class InternalPromotionApplicationListView(generics.ListAPIView):
    permission_classes = [CanReviewPromotionApplications]
    serializer_class = PromotionApplicationQueueSerializer
    pagination_class = PromotionApplicationPagination

    def get_queryset(self):
        queryset = PromotionApplication.objects.all()
        status_filter = str(self.request.query_params.get("status") or PromotionApplication.Status.PENDING_REVIEW).upper()
        if status_filter != "ALL":
            allowed = {value for value, _label in PromotionApplication.Status.choices}
            if status_filter not in allowed:
                raise ValidationError({"status": "Select a valid promotion application status."})
            queryset = queryset.filter(status=status_filter)
        promotion_type = str(self.request.query_params.get("promotion_type") or "").upper()
        if promotion_type:
            allowed_types = {value for value, _label in PromotionApplication.PromotionType.choices}
            if promotion_type not in allowed_types:
                raise ValidationError({"promotion_type": "Select a valid promotion type."})
            queryset = queryset.filter(promotion_type=promotion_type)
        queryset = _application_queryset(queryset)
        search = " ".join(str(self.request.query_params.get("q") or "").split())[:120]
        if search:
            queryset = queryset.filter(
                models.Q(artisan__business_name__icontains=search)
                | models.Q(artisan__user__first_name__icontains=search)
                | models.Q(artisan__user__last_name__icontains=search)
                | models.Q(state_country__icontains=search)
                | models.Q(state_name__icontains=search)
                | models.Q(institution__name__icontains=search)
                | models.Q(area_location__area__icontains=search)
            )
        return queryset.order_by("submitted_at", "created_at")

    @extend_schema(
        tags=["Promotion Review"],
        summary="List promotion applications for authorized Customer Support reviewers",
        parameters=[
            OpenApiParameter("status", str, OpenApiParameter.QUERY, description="Application status or ALL. Defaults to PENDING_REVIEW."),
            OpenApiParameter("promotion_type", str, OpenApiParameter.QUERY),
            OpenApiParameter("q", str, OpenApiParameter.QUERY),
        ],
        responses={200: PromotionApplicationQueueSerializer(many=True), 403: OpenApiResponse(description="Promotion review permission required.")},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class InternalPromotionApplicationDetailView(generics.RetrieveAPIView):
    permission_classes = [CanReviewPromotionApplications]
    serializer_class = PromotionApplicationReviewDetailSerializer
    lookup_url_kwarg = "pk"

    def get_queryset(self):
        return _application_queryset(PromotionApplication.objects.all())

    @extend_schema(
        tags=["Promotion Review"],
        summary="Review an application, its immutable submission history and public profile evidence",
        responses={200: PromotionApplicationReviewDetailSerializer, 403: OpenApiResponse(description="Promotion review permission required."), 404: OpenApiResponse(description="Application not found.")},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class InternalPromotionApplicationDecisionView(APIView):
    permission_classes = [CanReviewPromotionApplications]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    @extend_schema(
        tags=["Promotion Review"],
        summary="Approve, request changes to, or reject a pending application",
        request=PromotionDecisionSerializer,
        responses={200: PromotionApplicationReviewDetailSerializer, 400: OpenApiResponse(description="Invalid decision or current eligibility failed."), 403: OpenApiResponse(description="Promotion review permission required."), 404: OpenApiResponse(description="Application not found.")},
    )
    def post(self, request, pk):
        serializer = PromotionDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = get_object_or_404(PromotionApplication.objects.all(), pk=pk)
        updated, _decision = decide_promotion_application(
            application=application,
            reviewer=request.user,
            decision_type=serializer.validated_data["decision"],
            reason=serializer.validated_data["reason"],
        )
        fresh = _application_queryset(PromotionApplication.objects.filter(pk=updated.pk)).get()
        return Response(PromotionApplicationReviewDetailSerializer(fresh, context={"request": request}).data)


class PromotionApplicationEvidenceView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Promotion Applications"],
        summary="View an application work sample as its artisan or an authorized reviewer",
        responses={200: OpenApiResponse(description="Private promotion evidence image."), 403: OpenApiResponse(description="Evidence is private to the applicant and authorized reviewers."), 404: OpenApiResponse(description="Evidence not found.")},
    )
    def get(self, request, application_id, evidence_id):
        evidence = get_object_or_404(
            PromotionApplicationEvidence.objects.select_related("revision__application__artisan__user"),
            pk=evidence_id,
            revision__application_id=application_id,
        )
        application = evidence.revision.application
        try:
            is_owner = request.user.artisan_profile.pk == application.artisan_id
        except (AttributeError, ObjectDoesNotExist):
            is_owner = False
        is_reviewer = bool(request.user.is_active and user_has_support_role(request.user, PROMOTION_REVIEW_ROLES))
        if not (is_owner or is_reviewer):
            raise PermissionDenied("This work sample is private to the applicant and authorized Customer Support reviewers.")
        return FileResponse(
            evidence.image.open("rb"),
            content_type="image/webp",
            as_attachment=False,
            filename=evidence.image.name.rsplit("/", 1)[-1],
        )
