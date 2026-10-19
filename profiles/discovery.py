"""Public artisan discovery API: normalized filters, bounded pages and shared caching."""
import logging
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.cache import cache
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.utils.text import capfirst
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from home.models import ArtisanProfile, ArtisanService, Institution
from services.serializers import ArtisanServiceSerializer
from support.permissions import SupportRolePermission

from .discovery_cache import DISCOVERY_CACHE_TTL, discovery_cache_key
from .serializers import (
    ArtisanDiscoveryDetailSerializer,
    ArtisanDiscoverySerializer,
    ArtisanProfessionalLinksReviewSerializer,
)

logger = logging.getLogger(__name__)


class ArtisanDiscoveryPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 60


def _text_param(params, name, max_length):
    value = " ".join(str(params.get(name) or "").split())
    return value[:max_length]


def _boolean_param(params, name):
    raw = str(params.get(name) or "").strip().casefold()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValidationError({name: "Use true or false."})


def _uuid_param(params, name):
    raw = str(params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except (TypeError, ValueError, AttributeError):
        raise ValidationError({name: "Select a valid catalogue record."}) from None


def _rating_param(params, name):
    raw = str(params.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({name: "Enter a rating between 0 and 5."}) from None
    if not value.is_finite() or value < 0 or value > 5:
        raise ValidationError({name: "Enter a rating between 0 and 5."})
    return value


def _service_filter_queryset(params, institution, *, available_anywhere):
    """Return service rows matching catalogue and service-availability filters."""
    rows = ArtisanService.objects.filter(service__is_active=True)

    category_name = _text_param(params, "category_name", 100)
    if category_name:
        rows = rows.filter(service__category__name__iexact=category_name)

    service_name = _text_param(params, "service_name", 150)
    if service_name:
        rows = rows.filter(service__name__iexact=service_name)

    legacy_service_id = str(params.get("artisan_services__service") or "").strip()
    if legacy_service_id:
        if not legacy_service_id.isdigit():
            raise ValidationError({"artisan_services__service": "Select a valid service catalogue ID."})
        rows = rows.filter(service_id=int(legacy_service_id))

    legacy_category_id = _text_param(params, "artisan_services__service__category", 100)
    if legacy_category_id:
        rows = rows.filter(service__category_id=legacy_category_id)

    legacy_location_id = _uuid_param(params, "artisan_services__location")
    if legacy_location_id:
        rows = rows.filter(location_id=legacy_location_id)

    country = _text_param(params, "country", 100)
    state = _text_param(params, "state", 100)
    area = _text_param(params, "area", 150)
    has_location_filter = bool(country or state or area or institution)

    # A NULL service location is the established API contract for an offering
    # available anywhere; it is not the artisan's registered/physical location.
    local_match = Q(location__isnull=False)
    if country:
        local_match &= Q(location__country__iexact=country)
    if state:
        local_match &= Q(location__state__iexact=state)
    if area:
        local_match &= Q(location__area__iexact=area)
    if institution:
        local_match &= Q(location_id=institution.location_id)

    if available_anywhere is True:
        rows = rows.filter(location__isnull=True)
    elif available_anywhere is False:
        rows = rows.filter(location__isnull=False)
        if has_location_filter:
            rows = rows.filter(local_match)
    elif has_location_filter:
        rows = rows.filter(Q(location__isnull=True) | local_match)

    return rows


def _public_queryset():
    anywhere_offer = ArtisanService.objects.filter(
        artisan_id=OuterRef("pk"),
        service__is_active=True,
        location__isnull=True,
    )
    return (
        ArtisanProfile.objects
        .select_related("user", "verification")
        .filter(
            profile_status="ACTIVE",
            verification__verification_status="VERIFIED",
            user__is_active=True,
        )
        .annotate(
            available_anywhere=Exists(anywhere_offer),
            review_count=Count("reviews_received"),
        )
    )


def _profile_search_q(search):
    return (
        Q(user__first_name__icontains=search)
        | Q(user__last_name__icontains=search)
        | Q(business_name__icontains=search)
        | Q(bio__icontains=search)
    )


def _service_search_q(search):
    return (
        Q(service__name__icontains=search)
        | Q(service__category__name__icontains=search)
        | Q(location__area__icontains=search)
        | Q(location__state__icontains=search)
        | Q(location__country__icontains=search)
    )


@extend_schema_view(
    get=extend_schema(
        tags=["Artisan Discovery"],
        operation_id="artisan_discovery_list",
        description=(
            "Returns eligible public artisans with server-side search, service and "
            "service-location filters, stable institution IDs, ordering and bounded pagination."
        ),
        parameters=[
            OpenApiParameter("q", str, OpenApiParameter.QUERY, description="Search artisan names, business names, services and service locations."),
            OpenApiParameter("category_name", str, OpenApiParameter.QUERY),
            OpenApiParameter("service_name", str, OpenApiParameter.QUERY),
            OpenApiParameter("country", str, OpenApiParameter.QUERY, description="Filter by service availability; anywhere offers remain eligible."),
            OpenApiParameter("state", str, OpenApiParameter.QUERY),
            OpenApiParameter("area", str, OpenApiParameter.QUERY),
            OpenApiParameter("institution_id", str, OpenApiParameter.QUERY, description="UUID from the active institution catalogue."),
            OpenApiParameter("available_anywhere", str, OpenApiParameter.QUERY, description="true for anywhere offers; false for location-specific offers."),
        ],
        responses={
            200: ArtisanDiscoverySerializer(many=True),
            400: OpenApiResponse(description="A filter value is invalid."),
        },
    ),
)
class ArtisanDiscoveryListView(generics.ListAPIView):
    serializer_class = ArtisanDiscoverySerializer
    permission_classes = [AllowAny]
    pagination_class = ArtisanDiscoveryPagination
    filter_backends = [OrderingFilter]
    ordering_fields = ["average_rating", "completion_rate", "years_experience", "created_at"]
    ordering = ["-average_rating", "-completion_rate", "id"]
    is_discovery_list = True

    def list(self, request, *args, **kwargs):
        try:
            key = discovery_cache_key(request, self.paginator, default_ordering=self.ordering)
        except Exception:
            key = None
        if key:
            try:
                cached_data = cache.get(key)
            except Exception:
                cached_data = None
            if cached_data is not None:
                return Response(cached_data)

        response = super().list(request, *args, **kwargs)
        if key:
            try:
                cache.set(key, response.data, timeout=DISCOVERY_CACHE_TTL)
            except Exception:
                # Discovery remains available if the configured Redis service is unavailable.
                logger.warning("Artisan discovery cache write failed; serving uncached results.", exc_info=True)
        return response

    def get_queryset(self):
        params = self.request.query_params
        available_anywhere = _boolean_param(params, "available_anywhere")

        institution_id = _uuid_param(params, "institution_id")
        institution = None
        if institution_id:
            institution = (
                Institution.objects
                .select_related("location")
                .filter(pk=institution_id, is_active=True)
                .first()
            )
            if institution is None:
                raise ValidationError({"institution_id": "Select an active institution from the catalogue."})

        service_rows = _service_filter_queryset(
            params,
            institution,
            available_anywhere=available_anywhere,
        ).order_by("service__name", "pk")
        has_service_filter = any(
            _text_param(params, name, limit)
            for name, limit in (
                ("category_name", 100),
                ("service_name", 150),
                ("country", 100),
                ("state", 100),
                ("area", 150),
                ("artisan_services__service", 32),
                ("artisan_services__service__category", 100),
                ("artisan_services__location", 64),
            )
        ) or bool(institution) or available_anywhere is not None

        queryset = _public_queryset()
        if available_anywhere is False:
            anywhere_offers = ArtisanService.objects.filter(
                artisan_id=OuterRef("pk"), service__is_active=True, location__isnull=True
            )
            queryset = queryset.filter(~Exists(anywhere_offers))
        search = _text_param(params, "q", 160)
        if has_service_filter:
            queryset = queryset.filter(Exists(service_rows.filter(artisan_id=OuterRef("pk"))))

        if search:
            search_rows = service_rows.filter(_service_search_q(search)).filter(artisan_id=OuterRef("pk"))
            queryset = queryset.annotate(_discovery_service_search_match=Exists(search_rows)).filter(
                _profile_search_q(search) | Q(_discovery_service_search_match=True)
            )

        minimum_rating = _rating_param(params, "average_rating__gte")
        maximum_rating = _rating_param(params, "average_rating__lte")
        exact_rating = _rating_param(params, "average_rating")
        if minimum_rating is not None:
            queryset = queryset.filter(average_rating__gte=minimum_rating)
        if maximum_rating is not None:
            queryset = queryset.filter(average_rating__lte=maximum_rating)
        if exact_rating is not None:
            queryset = queryset.filter(average_rating=exact_rating)

        display_rows = service_rows.select_related("service__category", "location")
        if search:
            # Put service names/locations that match the search phrase first on the card,
            # while retaining other services when the phrase matched the artisan's name.
            from django.db.models import Case, IntegerField, Value, When
            display_rows = display_rows.annotate(
                _search_rank=Case(
                    When(_service_search_q(search), then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            ).order_by("_search_rank", "service__name", "pk")

        return queryset.prefetch_related(
            Prefetch("artisan_services", queryset=display_rows, to_attr="prefetched_artisan_services")
        ).order_by(*self.ordering)


@extend_schema_view(
    get=extend_schema(
        tags=["Artisan Discovery"],
        operation_id="artisan_discovery_detail",
        description=(
            "Returns a full public artisan profile. Optional professional social and portfolio "
            "links are exposed here only, never on discovery-list responses."
        ),
        responses={
            200: ArtisanDiscoveryDetailSerializer,
            404: OpenApiResponse(description="Artisan not found or not eligible for discovery."),
        },
    ),
)
class ArtisanDiscoveryDetailView(generics.RetrieveAPIView):
    serializer_class = ArtisanDiscoveryDetailSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        active_services = (
            ArtisanService.objects
            .filter(service__is_active=True)
            .select_related("service__category", "location")
            .order_by("service__name", "pk")
        )
        return _public_queryset().prefetch_related(
            Prefetch("artisan_services", queryset=active_services, to_attr="prefetched_artisan_services")
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Artisan Administration"],
        operation_id="artisan_professional_links_review",
        description="Restricted professional-link evidence view for authorized support staff and managers.",
        responses={200: ArtisanProfessionalLinksReviewSerializer},
    ),
)
class ArtisanProfessionalLinksReviewView(generics.RetrieveAPIView):
    serializer_class = ArtisanProfessionalLinksReviewSerializer
    permission_classes = [SupportRolePermission]
    queryset = ArtisanProfile.objects.select_related("user").all()
