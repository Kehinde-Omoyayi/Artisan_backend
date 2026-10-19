"""Public promotion placements, curation and campaign-performance API."""
import hashlib
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.audit import audit_request_action
from core.idempotency import idempotent_action
from home.models import ArtisanProfile, ArtisanService, Institution, Locations
from home.permissions import IsPlatformSuperuser
from profiles.discovery_cache import current_discovery_generation
from profiles.discovery import (
    _boolean_param,
    _profile_search_q,
    _rating_param,
    _service_filter_queryset,
    _service_search_q,
    _text_param,
    _uuid_param,
    _public_queryset,
)
from services.serializers import ArtisanServiceSerializer
from .models import (
    ArtisanMarketPick,
    PromotionApplication,
    PromotionCampaign,
    PromotionCampaignPerformanceEvent,
    PromotionInventory,
)
from .promotion_management_serializers import PromotionHomepageArtisanSerializer
from .placement_serializers import (
    MarketPickCandidateSerializer,
    MarketPickManagementActionSerializer,
    MarketPickManagementSerializer,
    MarketPickPublicSerializer,
    PromotionPerformanceEventSerializer,
)
from .placement_services import (
    PLACEMENT_TYPE_BY_CAMPAIGN,
    audit_market_pick_update,
    eligible_market_pick_artisans,
    eligible_public_active_campaigns,
    invalidate_promotion_selection_cache,
    make_performance_token,
    record_campaign_performance_event,
)

logger = logging.getLogger(__name__)


def _safe_selection_cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        logger.warning("Promotion selection cache read failed; bypassing cache.", exc_info=True)
        return None


def _safe_selection_cache_set(key, value, *, timeout=60):
    try:
        cache.set(key, value, timeout=timeout)
    except Exception:
        logger.warning("Promotion selection cache write failed; bypassing cache.", exc_info=True)


def _filter_value(value):
    return " ".join(str(value or "").split())


def _selected_geo_target(params, institution):
    """Build target predicates only from supported, exact catalogue relationships."""
    country = _text_param(params, "country", 100)
    state = _text_param(params, "state", 100)
    area = _text_param(params, "area", 150)
    explicit_location_id = _uuid_param(params, "area_location_id")
    no_targets = Q(pk__in=[])

    if institution:
        location = institution.location
        if any((
            country and country.casefold() != location.country.casefold(),
            state and state.casefold() != location.state.casefold(),
            area and area.casefold() != location.area.casefold(),
            explicit_location_id and explicit_location_id != location.pk,
        )):
            return no_targets, {}
        return (
            Q(promotion_type=PromotionApplication.PromotionType.INSTITUTION, inventory__institution_id=institution.pk)
            | Q(promotion_type=PromotionApplication.PromotionType.AREA, inventory__area_location_id=location.pk)
            | Q(
                promotion_type=PromotionApplication.PromotionType.STATE,
                inventory__state_country__iexact=location.country,
                inventory__state_name__iexact=location.state,
            ),
            {
                PromotionApplication.PromotionType.INSTITUTION: 3,
                PromotionApplication.PromotionType.AREA: 2,
                PromotionApplication.PromotionType.STATE: 1,
            },
        )

    if explicit_location_id:
        location = Locations.objects.filter(pk=explicit_location_id).first()
        if not location:
            raise ValidationError({"area_location_id": "Select a supported canonical service-area record."})
        if any((
            country and country.casefold() != location.country.casefold(),
            state and state.casefold() != location.state.casefold(),
            area and area.casefold() != location.area.casefold(),
        )):
            return no_targets, {}
        return (
            Q(promotion_type=PromotionApplication.PromotionType.AREA, inventory__area_location_id=location.pk)
            | Q(
                promotion_type=PromotionApplication.PromotionType.STATE,
                inventory__state_country__iexact=location.country,
                inventory__state_name__iexact=location.state,
            ),
            {PromotionApplication.PromotionType.AREA: 2, PromotionApplication.PromotionType.STATE: 1},
        )

    if area:
        locations = Locations.objects.filter(area__iexact=area)
        if country:
            locations = locations.filter(country__iexact=country)
        if state:
            locations = locations.filter(state__iexact=state)
        # A locality name without enough filters may refer to multiple real places.
        targets = list(locations.values("country", "state", "area").distinct()[:2])
        if len(targets) != 1:
            return no_targets, {}
        target = targets[0]
        target_country, target_state, target_area = target["country"], target["state"], target["area"]
        area_ids = locations.filter(
            country__iexact=target_country,
            state__iexact=target_state,
            area__iexact=target_area,
        ).values("pk")
        return (
            Q(
                promotion_type=PromotionApplication.PromotionType.AREA,
                inventory__area_location_id__in=area_ids,
            )
            | Q(
                promotion_type=PromotionApplication.PromotionType.STATE,
                inventory__state_country__iexact=target_country,
                inventory__state_name__iexact=target_state,
            ),
            {PromotionApplication.PromotionType.AREA: 2, PromotionApplication.PromotionType.STATE: 1},
        )

    if state:
        locations = Locations.objects.filter(state__iexact=state)
        if country:
            locations = locations.filter(country__iexact=country)
        targets = list(locations.values("country", "state").distinct()[:2])
        if len(targets) != 1:
            return no_targets, {}
        target_country, target_state = targets[0]["country"], targets[0]["state"]
        return (
            Q(
                promotion_type=PromotionApplication.PromotionType.STATE,
                inventory__state_country__iexact=target_country,
                inventory__state_name__iexact=target_state,
            ),
            {PromotionApplication.PromotionType.STATE: 1},
        )

    # Country-only and unfiltered discovery deliberately show no geographic ads.
    return no_targets, {}


def _profile_text_matches(artisan, query):
    query = query.casefold()
    user = artisan.user
    return any(
        query in str(value or "").casefold()
        for value in (user.first_name, user.last_name, artisan.business_name, artisan.bio)
    )


def _service_text_matches(offering, query):
    query = query.casefold()
    location = offering.location
    return any(
        query in str(value or "").casefold()
        for value in (
            offering.service.name,
            offering.service.category.name,
            location.area if location else "",
            location.state if location else "",
            location.country if location else "",
        )
    )


def _campaign_service_matches(campaign, offers_by_artisan, query):
    service_ids = {
        item.get("artisan_service_id")
        for item in (campaign.services_snapshot if isinstance(campaign.services_snapshot, list) else [])
        if isinstance(item, dict) and item.get("artisan_service_id") is not None
    }
    matching = [row for row in offers_by_artisan.get(campaign.artisan_id, []) if row.pk in service_ids]
    if not matching:
        return []
    if query and not _profile_text_matches(campaign.artisan, query) and not any(_service_text_matches(row, query) for row in matching):
        return []
    return matching


def _selection_cache_key(filters, *, generation, minute_bucket, display_limit):
    payload = {
        "generation": generation,
        "filters": filters,
        "minute_bucket": minute_bucket,
        "display_limit": display_limit,
        "rule_set": "promotion-canonical-target-specificity-fair-exposure-v1",
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"promotion-discovery:promotion:{generation}:{digest}"


class PromotionDiscoveryView(APIView):
    """Small, backend-selected paid result section separate from ordinary discovery."""
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Public Promotions"],
        summary="Get a small, rotated paid selection eligible for the supplied discovery filters",
        parameters=[
            OpenApiParameter("q", str, OpenApiParameter.QUERY),
            OpenApiParameter("category_name", str, OpenApiParameter.QUERY),
            OpenApiParameter("service_name", str, OpenApiParameter.QUERY),
            OpenApiParameter("country", str, OpenApiParameter.QUERY),
            OpenApiParameter("state", str, OpenApiParameter.QUERY),
            OpenApiParameter("area", str, OpenApiParameter.QUERY),
            OpenApiParameter("area_location_id", str, OpenApiParameter.QUERY, description="Optional canonical Locations UUID."),
            OpenApiParameter("institution_id", str, OpenApiParameter.QUERY),
            OpenApiParameter("available_anywhere", str, OpenApiParameter.QUERY),
        ],
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiResponse(description="A filter value is invalid.")},
    )
    def get(self, request):
        params = request.query_params
        available_anywhere = _boolean_param(params, "available_anywhere")
        institution_id = _uuid_param(params, "institution_id")
        institution = None
        if institution_id:
            institution = Institution.objects.select_related("location").filter(pk=institution_id, is_active=True).first()
            if institution is None:
                raise ValidationError({"institution_id": "Select an active institution from the supported catalogue."})

        query_filters = {
            key: _text_param(params, key, max_length)
            for key, max_length in (
                ("q", 160), ("category_name", 100), ("service_name", 150),
                ("country", 100), ("state", 100), ("area", 150),
                ("institution_id", 64), ("area_location_id", 64),
                ("available_anywhere", 10), ("average_rating", 20),
                ("average_rating__gte", 20), ("average_rating__lte", 20),
                ("artisan_services__service", 32),
                ("artisan_services__service__category", 100),
                ("artisan_services__location", 64),
            )
        }
        geo_target, specificity_by_type = _selected_geo_target(params, institution)
        if not specificity_by_type:
            return Response({"count": 0, "display_limit": max(0, int(getattr(settings, "PROMOTION_DISCOVERY_DISPLAY_LIMIT", 3))), "results": []})

        service_rows = _service_filter_queryset(
            params,
            institution,
            available_anywhere=available_anywhere,
        ).order_by("service__name", "pk")
        eligible_artisans = _public_queryset().filter(
            Exists(service_rows.filter(artisan_id=OuterRef("pk")))
        )
        minimum_rating = _rating_param(params, "average_rating__gte")
        maximum_rating = _rating_param(params, "average_rating__lte")
        exact_rating = _rating_param(params, "average_rating")
        if minimum_rating is not None:
            eligible_artisans = eligible_artisans.filter(average_rating__gte=minimum_rating)
        if maximum_rating is not None:
            eligible_artisans = eligible_artisans.filter(average_rating__lte=maximum_rating)
        if exact_rating is not None:
            eligible_artisans = eligible_artisans.filter(average_rating=exact_rating)

        query = _text_param(params, "q", 160)
        if query:
            service_search_rows = service_rows.filter(_service_search_q(query)).filter(artisan_id=OuterRef("pk"))
            eligible_artisans = eligible_artisans.filter(
                _profile_search_q(query) | Exists(service_search_rows)
            )

        now = timezone.now()
        campaign_rows = eligible_public_active_campaigns(
            now=now,
            queryset=PromotionCampaign.objects.filter(
                geo_target,
                promotion_type__in=list(specificity_by_type),
                artisan_id__in=eligible_artisans.values("pk"),
            ).annotate(promotion_impressions=F("performance_impressions")).order_by("payment_verified_at", "id"),
        )
        if not campaign_rows:
            return Response({"count": 0, "display_limit": max(0, int(getattr(settings, "PROMOTION_DISCOVERY_DISPLAY_LIMIT", 3))), "results": []})

        matching_offers = list(
            service_rows.filter(artisan_id__in={item.artisan_id for item in campaign_rows})
            .select_related("service__category", "location")
            .order_by("service__name", "pk")
        )
        offers_by_artisan = defaultdict(list)
        for offer in matching_offers:
            offers_by_artisan[offer.artisan_id].append(offer)

        candidates = []
        for campaign in campaign_rows:
            matched_offers = _campaign_service_matches(campaign, offers_by_artisan, query)
            if not matched_offers:
                continue
            candidates.append({
                "campaign": campaign,
                "services": matched_offers,
                "specificity": specificity_by_type[campaign.promotion_type],
            })
        if not candidates:
            return Response({"count": 0, "display_limit": max(0, int(getattr(settings, "PROMOTION_DISCOVERY_DISPLAY_LIMIT", 3))), "results": []})

        minute_bucket = int(now.timestamp() // 60)
        def fairness_tie(candidate):
            identity = str(candidate["campaign"].pk)
            return hashlib.sha256(f"promotion:{minute_bucket}:{identity}".encode()).hexdigest()

        # If the same artisan bought several eligible products, expose only the most
        # specific placement for that search. Comparable targets rotate by aggregate
        # impressions, then by a short time-bucketed deterministic tie-breaker.
        best_by_artisan = {}
        for candidate in candidates:
            campaign = candidate["campaign"]
            rank = (-candidate["specificity"], campaign.promotion_impressions, fairness_tie(candidate), str(campaign.pk))
            existing = best_by_artisan.get(campaign.artisan_id)
            if existing is None:
                best_by_artisan[campaign.artisan_id] = (rank, candidate)
            elif rank < existing[0]:
                best_by_artisan[campaign.artisan_id] = (rank, candidate)
        ranked = [item[1] for item in best_by_artisan.values()]
        ranked.sort(key=lambda item: (
            -item["specificity"], item["campaign"].promotion_impressions,
            fairness_tie(item), str(item["campaign"].pk),
        ))

        configured_limit = max(0, int(getattr(settings, "PROMOTION_DISCOVERY_DISPLAY_LIMIT", 3)))
        display_limit = min(configured_limit, 50)
        if display_limit == 0:
            return Response({"count": 0, "display_limit": 0, "results": []})
        generation = current_discovery_generation()
        selected = ranked[:display_limit]
        if generation is not None:
            key = _selection_cache_key(
                query_filters,
                generation=generation,
                minute_bucket=minute_bucket,
                display_limit=display_limit,
            )
            cached_ids = _safe_selection_cache_get(key)
            if isinstance(cached_ids, list):
                by_id = {str(item["campaign"].pk): item for item in ranked}
                selected = [by_id[value] for value in cached_ids if value in by_id]
                selected_ids = {str(item["campaign"].pk) for item in selected}
                if len(selected) < display_limit:
                    selected.extend(item for item in ranked if str(item["campaign"].pk) not in selected_ids)
                    selected = selected[:display_limit]
            else:
                _safe_selection_cache_set(key, [str(item["campaign"].pk) for item in selected], timeout=60)

        results = []
        for candidate in selected:
            campaign = candidate["campaign"]
            row = dict(PromotionHomepageArtisanSerializer(campaign, context={"request": request}).data)
            row.update({
                "placement_type": PLACEMENT_TYPE_BY_CAMPAIGN[campaign.promotion_type],
                "promotion_label": "Promoted",
                "target_label": campaign.inventory.target_label,
                "specificity": candidate["specificity"],
                "main_service": candidate["services"][0].service.name,
                "services": ArtisanServiceSerializer(candidate["services"][:3], many=True, context={"request": request}).data,
                "impression_token": make_performance_token(campaign, event_type=PromotionCampaignPerformanceEvent.EventType.IMPRESSION),
                "profile_visit_token": make_performance_token(campaign, event_type=PromotionCampaignPerformanceEvent.EventType.PROFILE_VISIT),
            })
            results.append(row)
        return Response({"count": len(results), "display_limit": display_limit, "results": results})


class PromotionPerformanceEventView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "promotion_performance"

    @extend_schema(
        tags=["Public Promotions"],
        summary="Record one first-party, deduplicated impression or promoted profile visit",
        request=PromotionPerformanceEventSerializer,
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, 400: OpenApiResponse(description="Invalid or expired token."), 429: OpenApiResponse(description="Performance event rate limit reached.")},
    )
    def post(self, request):
        serializer = PromotionPerformanceEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recorded = record_campaign_performance_event(
            token=serializer.validated_data["token"],
            visitor_id=serializer.validated_data["visitor_id"],
            expected_event_type=serializer.validated_data["event_type"],
            expected_artisan_id=request.data.get("artisan_id"),
        )
        return Response({"recorded": recorded}, status=status.HTTP_201_CREATED if recorded else status.HTTP_200_OK)


def _prefetch_market_pick_services(artisans):
    artisan_ids = [item.pk for item in artisans]
    service_rows = ArtisanService.objects.filter(
        artisan_id__in=artisan_ids,
        service__is_active=True,
    ).select_related("service__category", "location").order_by("service__name", "pk")
    services_by_artisan = defaultdict(list)
    for row in service_rows:
        services_by_artisan[row.artisan_id].append(row)
    for artisan in artisans:
        artisan.market_pick_services = services_by_artisan.get(artisan.pk, [])
    return artisans


class PublicArtisanMarketPicksView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(tags=["Public Promotions"], summary="List currently eligible platform-selected Artisan Market Picks", responses={200: MarketPickPublicSerializer(many=True)})
    def get(self, request):
        eligible_artisans = eligible_market_pick_artisans()
        eligible_by_id = {item.pk: item for item in eligible_artisans}
        eligible_ids = set(eligible_by_id)
        generation = current_discovery_generation()
        configured_limit = min(max(int(getattr(settings, "PROMOTION_MARKET_PICKS_DISPLAY_LIMIT", 6)), 0), 50)
        if not configured_limit or not eligible_ids:
            return Response([])
        cache_key = f"promotion-market-picks:promotion:{generation}" if generation is not None else None
        cached_ids = _safe_selection_cache_get(cache_key) if cache_key else None
        if isinstance(cached_ids, list):
            ordered_ids = [UUID(value) for value in cached_ids if value in {str(pk) for pk in eligible_ids}]
        else:
            ordered_ids = list(
                ArtisanMarketPick.objects.filter(is_active=True, artisan_id__in=eligible_ids)
                .order_by("position", "selected_at", "id")
                .values_list("artisan_id", flat=True)[:configured_limit]
            )
            if cache_key:
                _safe_selection_cache_set(cache_key, [str(value) for value in ordered_ids], timeout=60)
        if not ordered_ids:
            return Response([])
        artisans = _prefetch_market_pick_services([eligible_by_id[artisan_id] for artisan_id in ordered_ids])
        by_id = {item.pk: item for item in artisans}
        picks = list(
            ArtisanMarketPick.objects.filter(is_active=True, artisan_id__in=by_id)
            .select_related("artisan__user")
            .order_by("position", "selected_at", "id")
        )
        for pick in picks:
            pick.artisan = by_id[pick.artisan_id]
        return Response(MarketPickPublicSerializer(picks, many=True, context={"request": request}).data)


class ArtisanMarketPicksManagementView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(
        tags=["Promotion Management"],
        request=None,
        responses={
            200: inline_serializer(
                name="MarketPickManagementListResponse",
                fields={
                    "selected_picks": MarketPickManagementSerializer(many=True),
                    "eligible_artisans": MarketPickCandidateSerializer(many=True),
                    "eligible_candidate_count": serializers.IntegerField(),
                },
            ),
        },
    )
    def get(self, request):
        candidates = eligible_market_pick_artisans()
        candidate_ids = {item.pk for item in candidates}
        _prefetch_market_pick_services(candidates)
        candidate_rows = candidates[:200]
        picks = list(
            ArtisanMarketPick.objects.select_related("artisan__user")
            .annotate(
                artisan_review_count=Count("artisan__reviews_received", distinct=True),
                artisan_completed_booking_count=Count(
                    "artisan__booking",
                    filter=Q(artisan__booking__status="COMPLETED"),
                    distinct=True,
                ),
            )
            .order_by("-is_active", "position", "created_at", "id")
        )
        _prefetch_market_pick_services([pick.artisan for pick in picks])
        for pick in picks:
            pick.currently_eligible = pick.artisan_id in candidate_ids
        return Response({
            "selected_picks": MarketPickManagementSerializer(picks, many=True).data,
            "eligible_artisans": MarketPickCandidateSerializer(candidate_rows, many=True).data,
            "eligible_candidate_count": len(candidates),
        })

    @extend_schema(
        tags=["Promotion Management"],
        summary="Curate the separate, unpaid Artisan Market Picks editorial section",
        request=MarketPickManagementActionSerializer,
        responses={200: MarketPickManagementSerializer, 400: OpenApiResponse(description="The artisan is not currently eligible for editorial selection."), 403: OpenApiResponse(description="Platform manager access required.")},
    )
    @idempotent_action("promotion.market-pick.manage", resource_type="ArtisanMarketPick")
    def post(self, request):
        serializer = MarketPickManagementActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        action = values["action"]
        artisan_id = values["artisan_id"]
        with transaction.atomic():
            pick = ArtisanMarketPick.objects.select_for_update(of=("self",)).filter(artisan_id=artisan_id).first()
            if action == "REMOVE":
                if pick is None:
                    raise ValidationError({"artisan_id": "This artisan is not currently selected."})
                before = {"is_active": pick.is_active, "position": pick.position, "selection_note": pick.selection_note}
                pick.is_active = False
                pick.selection_note = values["selection_note"].strip()
                pick.selected_by = request.user
                pick.save(update_fields=["is_active", "selection_note", "selected_by", "updated_at"])
            else:
                candidate = next(
                    (item for item in eligible_market_pick_artisans() if item.pk == artisan_id),
                    None,
                )
                if candidate is None:
                    raise ValidationError({"artisan_id": "Only a currently verified, complete, active artisan with at least one active service can be selected."})
                if pick is None:
                    try:
                        with transaction.atomic():
                            pick = ArtisanMarketPick.objects.create(artisan_id=artisan_id)
                    except IntegrityError:
                        pick = ArtisanMarketPick.objects.select_for_update(of=("self",)).get(artisan_id=artisan_id)
                before = {"is_active": pick.is_active, "position": pick.position, "selection_note": pick.selection_note}
                was_active = pick.is_active
                pick.position = values.get("position", pick.position or 1)
                pick.selection_note = values.get("selection_note", pick.selection_note).strip()
                pick.is_active = True
                pick.selected_by = request.user
                if not was_active or pick.selected_at is None:
                    pick.selected_at = timezone.now()
                pick.save(update_fields=["position", "selection_note", "is_active", "selected_by", "selected_at", "updated_at"])
            audit_market_pick_update(request=request, pick=pick, action=action, before=before)
            transaction.on_commit(invalidate_promotion_selection_cache)
        pick = (
            ArtisanMarketPick.objects.select_related("artisan__user")
            .annotate(
                artisan_review_count=Count("artisan__reviews_received", distinct=True),
                artisan_completed_booking_count=Count("artisan__booking", filter=Q(artisan__booking__status="COMPLETED"), distinct=True),
            )
            .get(pk=pick.pk)
        )
        _prefetch_market_pick_services([pick.artisan])
        pick.currently_eligible = any(item.pk == pick.artisan_id for item in eligible_market_pick_artisans())
        return Response(MarketPickManagementSerializer(pick).data)
