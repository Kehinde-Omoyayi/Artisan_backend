from decimal import Decimal
import hashlib

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import audit_request_action
from core.models import AuditLog
from core.idempotency import idempotent_action
from home.permissions import IsPlatformSuperuser
from profiles.discovery_cache import invalidate_public_discovery_cache
from support.models import SupportCase
from support.permissions import (
    SUPPORT_ADMIN,
    SUPPORT_SUPERVISOR,
    user_has_support_role,
)
from .campaign_services import (
    create_checkout_offer,
    estimate_earliest_start,
    ensure_homepage_inventory,
    pause_or_restore_or_revoke_campaign,
    recompute_inventory_schedule,
    start_campaign_checkout,
)
from .models import (
    PromotionApplication,
    PromotionCampaign,
    PromotionCampaignAction,
    PromotionCampaignPerformanceEvent,
    PromotionInventory,
    PromotionPayment,
    PromotionPlan,
)
from .permissions import CanReviewPromotionApplications, IsArtisanApplicant
from .placement_services import (
    PLACEMENT_TYPE_BY_CAMPAIGN,
    active_snapshotted_service_ids,
    eligible_public_active_campaigns,
    make_performance_token,
    public_active_campaign_counts_by_inventory,
)
from .promotion_management_serializers import (
    PromotionCampaignActionSerializer,
    PromotionCampaignApplicantSerializer,
    PromotionCampaignManagementSerializer,
    PromotionHomepageArtisanSerializer,
    PromotionInventoryCapacitySerializer,
    PromotionInventorySerializer,
    PromotionOfferSerializer,
    PromotionPaymentApplicantSerializer,
    PromotionPlanAdminSerializer,
    PromotionPlanUpdateSerializer,
    PromotionCheckoutSerializer,
)


class PromotionOfferView(APIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]

    @extend_schema(tags=["Promotion Campaigns"], summary="Get the current manager-configured offer for one approved application", responses={200: PromotionOfferSerializer, 409: OpenApiResponse(description="The application, plan or target is not purchasable.")})
    def get(self, request, pk):
        application = get_object_or_404(
            PromotionApplication.objects.select_related("artisan", "institution__location", "area_location").filter(artisan=request.user.artisan_profile),
            pk=pk,
        )
        return Response(create_checkout_offer(application))


class PromotionCheckoutView(APIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]

    @extend_schema(tags=["Promotion Campaigns"], summary="Accept the exact approved offer and queue Flutterwave payment initialization", request=PromotionCheckoutSerializer, responses={202: OpenApiTypes.OBJECT, 200: OpenApiTypes.OBJECT, 400: OpenApiResponse(description="The offer or terms are invalid."), 409: OpenApiResponse(description="The application or plan changed.")}, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)])
    @idempotent_action("promotion.checkout", resource_type="PromotionCampaign")
    def post(self, request, pk):
        application = get_object_or_404(
            PromotionApplication.objects.select_related("artisan", "institution__location", "area_location").filter(artisan=request.user.artisan_profile),
            pk=pk,
        )
        serializer = PromotionCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            campaign, payment, created = start_campaign_checkout(
                application,
                user=request.user,
                quote_token=serializer.validated_data["quote_token"],
                accepted_terms=serializer.validated_data["accepted_terms"],
                accepted_terms_version=serializer.validated_data["accepted_terms_version"],
            )
        except ValidationError:
            raise
        if created:
            transaction.on_commit(lambda payment_id=str(payment.id): _enqueue_promotion_payment(payment_id))
        payload = {
            "campaign_id": str(campaign.id),
            "payment_id": str(payment.id),
            "reference": payment.reference,
            "amount": str(Decimal(payment.amount_kobo) / Decimal(100)),
            "currency": payment.currency,
            "payment_status": payment.status,
            "campaign_status": campaign.status,
            "initialization_pending": payment.status == PromotionPayment.Status.INITIALIZATION_PENDING,
            "payment_status_url": f"/api/v1/promotions/payments/{payment.id}/",
        }
        return Response(payload, status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK)


def _enqueue_promotion_payment(payment_id):
    try:
        from .tasks import initialize_promotion_payment
        initialize_promotion_payment.delay(payment_id)
    except Exception:
        # The periodic reconciliation task will pick up durable, pending payment rows.
        import logging
        logging.getLogger(__name__).exception("promotion_payment_initialization_enqueue_failed payment=%s", payment_id)


class MyPromotionCampaignsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]
    serializer_class = PromotionCampaignApplicantSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PromotionCampaign.objects.none()
        return (
            PromotionCampaign.objects.filter(artisan=self.request.user.artisan_profile)
            .select_related("application", "inventory", "verified_payment", "artisan__user")
            .prefetch_related("payments", "actions")
            .order_by("-created_at", "id")
        )

    @extend_schema(tags=["Promotion Campaigns"], summary="List current-user campaigns, payment history, schedule, queue and performance stats", responses={200: PromotionCampaignApplicantSerializer(many=True)})
    def list(self, request, *args, **kwargs):
        campaigns = list(self.get_queryset())
        inventory_ids = {item.inventory_id for item in campaigns}
        waiting_by_inventory = {}
        all_waiting = PromotionCampaign.objects.filter(
            inventory_id__in=inventory_ids,
            status__in=[PromotionCampaign.Status.QUEUED, PromotionCampaign.Status.SCHEDULED],
            payment_verified_at__isnull=False,
        ).only("id", "inventory_id", "queue_reentry_at", "payment_verified_at")
        for item in all_waiting:
            waiting_by_inventory.setdefault(item.inventory_id, []).append(item)
        positions = {}
        for items in waiting_by_inventory.values():
            items.sort(key=lambda item: (item.queue_reentry_at or item.payment_verified_at, str(item.id)))
            positions.update({str(item.id): index + 1 for index, item in enumerate(items)})
        for campaign in campaigns:
            payment_rows = list(campaign.payments.all())
            campaign.latest_payment_obj = payment_rows[-1] if payment_rows else None
            campaign.prefetched_campaign_actions = list(campaign.actions.all())[:30]
        data = self.get_serializer(campaigns, many=True, context={"request": request, "queue_positions": positions}).data
        return Response(data)


class PromotionPaymentStatusView(APIView):
    permission_classes = [IsAuthenticated, IsArtisanApplicant]

    @extend_schema(tags=["Promotion Campaigns"], summary="Get current-user promotion payment initialization and trusted verification status", responses={200: OpenApiTypes.OBJECT, 404: OpenApiResponse(description="Payment not found.")})
    def get(self, request, pk):
        payment = get_object_or_404(
            PromotionPayment.objects.select_related("campaign__inventory", "campaign__artisan"),
            pk=pk,
            campaign__artisan=request.user.artisan_profile,
        )
        campaign = payment.campaign
        return Response({
            **PromotionPaymentApplicantSerializer(payment, context={"request": request}).data,
            "campaign_id": str(campaign.id),
            "campaign_status": campaign.status,
            "scheduled_start_at": campaign.scheduled_start_at,
            "started_at": campaign.started_at,
            "expires_at": campaign.expires_at,
            "target_label": campaign.inventory.target_label,
        })


class PublicHomepagePromotionsView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(tags=["Public Promotions"], summary="List anonymous-visitor eligible paid homepage promotions", responses={200: PromotionHomepageArtisanSerializer(many=True)})
    def get(self, request):
        now = timezone.now()
        inventory = PromotionInventory.objects.filter(
            promotion_type=PromotionApplication.PromotionType.HOMEPAGE,
            target_key="HOMEPAGE",
        ).first()
        if not inventory or inventory.capacity <= 0:
            return Response([])
        display_limit = min(max(int(getattr(settings, "PROMOTION_HOMEPAGE_DISPLAY_LIMIT", 3)), 0), 50)
        if display_limit == 0:
            return Response([])
        campaigns = eligible_public_active_campaigns(
            now=now,
            queryset=PromotionCampaign.objects.filter(
                inventory=inventory,
                promotion_type=PromotionApplication.PromotionType.HOMEPAGE,
            ).annotate(promotion_impressions=F("performance_impressions")).order_by("payment_verified_at", "id"),
        )
        minute_bucket = int(now.timestamp() // 60)
        campaigns.sort(key=lambda campaign: (
            campaign.promotion_impressions,
            hashlib.sha256(f"homepage:{minute_bucket}:{campaign.pk}".encode()).hexdigest(),
            str(campaign.pk),
        ))
        results = []
        seen_artisans = set()
        for campaign in campaigns:
            if campaign.artisan_id in seen_artisans:
                continue
            active_ids = active_snapshotted_service_ids(campaign)
            services = [
                item for item in (campaign.services_snapshot if isinstance(campaign.services_snapshot, list) else [])
                if isinstance(item, dict) and item.get("artisan_service_id") in active_ids
            ]
            row = dict(PromotionHomepageArtisanSerializer(campaign, context={"request": request}).data)
            row.update({
                "placement_type": PLACEMENT_TYPE_BY_CAMPAIGN[campaign.promotion_type],
                "promotion_label": "Sponsored",
                "main_service": services[0].get("service_name", "") if services else "",
                "services": services,
                "impression_token": make_performance_token(campaign, event_type=PromotionCampaignPerformanceEvent.EventType.IMPRESSION),
                "profile_visit_token": make_performance_token(campaign, event_type=PromotionCampaignPerformanceEvent.EventType.PROFILE_VISIT),
            })
            results.append(row)
            seen_artisans.add(campaign.artisan_id)
            if len(results) >= display_limit:
                break
        return Response(results)


@method_decorator(never_cache, name="dispatch")
class PromotionPlansManagementView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(
        tags=["Promotion Management"],
        operation_id="promotion_plan_list",
        summary="List configured promotion prices, durations, and terms",
        request=None,
        responses={200: PromotionPlanAdminSerializer(many=True)},
    )
    def get(self, request):
        plans = PromotionPlan.objects.select_related("updated_by").all()
        return Response(PromotionPlanAdminSerializer(plans, many=True).data)


@method_decorator(never_cache, name="dispatch")
class PromotionPlanManagementDetailView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(
        tags=["Promotion Management"],
        operation_id="promotion_plan_retrieve",
        summary="Retrieve one configured promotion plan",
        request=None,
        responses={
            200: PromotionPlanAdminSerializer,
            404: OpenApiResponse(description="Promotion plan not found."),
        },
    )
    def get(self, request, pk):
        plan = get_object_or_404(
            PromotionPlan.objects.select_related("updated_by"),
            pk=pk,
        )
        return Response(PromotionPlanAdminSerializer(plan).data)

    @extend_schema(
        tags=["Promotion Management"],
        operation_id="promotion_plan_partial_update",
        request=PromotionPlanUpdateSerializer,
        responses={
            200: PromotionPlanAdminSerializer,
            400: OpenApiResponse(description="Plan settings are invalid."),
            404: OpenApiResponse(description="Promotion plan not found."),
        },
    )
    @idempotent_action("promotion.plan.update", resource_type="PromotionPlan")
    def patch(self, request, pk):
        with transaction.atomic():
            plan = get_object_or_404(
                PromotionPlan.objects.select_for_update(of=("self",)),
                pk=pk,
            )
            serializer = PromotionPlanUpdateSerializer(plan, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            before = {
                "price_kobo": plan.price_kobo,
                "duration_days": plan.duration_days,
                "is_enabled": plan.is_enabled,
                "terms_text": plan.terms_text,
                "refund_policy": plan.refund_policy,
                "name": plan.name,
            }
            values = serializer.validated_data
            for field in ("name", "is_enabled", "duration_days", "refund_policy", "terms_text"):
                if field in values:
                    setattr(plan, field, values[field])
            if "price" in values:
                plan.price_kobo = int(values["price"] * 100)
            after = {
                "price_kobo": plan.price_kobo,
                "duration_days": plan.duration_days,
                "is_enabled": plan.is_enabled,
                "terms_text": plan.terms_text,
                "refund_policy": plan.refund_policy,
                "name": plan.name,
            }
            if before != after:
                plan.version += 1
            plan.updated_by = request.user
            plan.save()
            if before != after:
                actor_name = request.user.get_full_name() or request.user.email
                change_reason = values.get("reason") or "Authorized platform manager updated promotion plan configuration."
                audit_request_action(
                    request=request,
                    action="PROMOTION_PLAN_CONFIGURED",
                    resource_type="PromotionPlan",
                    resource_id=str(plan.id),
                    metadata={
                        "before": before,
                        "after": after,
                        "version": plan.version,
                        "actor_name_snapshot": actor_name[:160],
                        "reason": change_reason,
                    },
                )
        return Response(PromotionPlanAdminSerializer(plan).data)


@method_decorator(never_cache, name="dispatch")
class PromotionInventoryManagementView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]
    serializer_class = PromotionInventorySerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PromotionInventory.objects.none()
        ensure_homepage_inventory()
        counts = {
            "scheduled_count": Count(
                "campaigns",
                filter=Q(campaigns__status=PromotionCampaign.Status.SCHEDULED, campaigns__payment_verified_at__isnull=False),
            ),
            "queued_count": Count(
                "campaigns",
                filter=Q(campaigns__status=PromotionCampaign.Status.QUEUED, campaigns__payment_verified_at__isnull=False),
            ),
        }
        queryset = PromotionInventory.objects.annotate(**counts).order_by("promotion_type", "target_label")
        promotion_type = self.request.query_params.get("promotion_type")
        return queryset.filter(promotion_type=promotion_type) if promotion_type else queryset

    @extend_schema(tags=["Promotion Management"], summary="List separately keyed target inventories and live capacity usage", responses={200: PromotionInventorySerializer(many=True)})
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        inventories = list(page if page is not None else queryset)
        active_counts = public_active_campaign_counts_by_inventory(now=timezone.now())
        for inventory in inventories:
            inventory.active_count = active_counts.get(inventory.pk, 0)
        serializer = self.get_serializer(inventories, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


@method_decorator(never_cache, name="dispatch")
class PromotionInventoryCapacityView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(tags=["Promotion Management"], request=PromotionInventoryCapacitySerializer, responses={200: PromotionInventorySerializer})
    @idempotent_action("promotion.inventory.capacity.update", resource_type="PromotionInventory")
    def patch(self, request, pk):
        serializer = PromotionInventoryCapacitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            inventory = get_object_or_404(PromotionInventory.objects.select_for_update(of=("self",)), pk=pk)
            before = inventory.capacity
            inventory.capacity = serializer.validated_data["capacity"]
            if before != inventory.capacity:
                inventory.save(update_fields=["capacity", "updated_at"])
                actor_name = request.user.get_full_name() or request.user.email
                change_reason = serializer.validated_data.get("reason") or "Authorized platform manager updated target capacity."
                audit_request_action(
                    request=request,
                    action="PROMOTION_INVENTORY_CAPACITY_CHANGED",
                    resource_type="PromotionInventory",
                    resource_id=str(inventory.id),
                    metadata={
                        "target_key": inventory.target_key,
                        "capacity_before": before,
                        "capacity_after": inventory.capacity,
                        "actor_name_snapshot": actor_name[:160],
                        "reason": change_reason,
                    },
                )
                transaction.on_commit(
                    lambda inventory_id=str(inventory.id), actor=request.user, old_capacity=before, new_capacity=inventory.capacity, name=actor_name, reason=change_reason: recompute_inventory_schedule(
                        inventory_id,
                        actor=actor,
                        correction_reason=f"{reason} Authorized target-capacity correction by {name}: capacity changed from {old_capacity} to {new_capacity}.",
                    )
                )
                transaction.on_commit(invalidate_public_discovery_cache)
        inventory = self.get_inventory(pk)
        return Response(PromotionInventorySerializer(inventory).data)

    def get_inventory(self, pk):
        inventory = PromotionInventory.objects.annotate(
            scheduled_count=Count(
                "campaigns",
                filter=Q(campaigns__status=PromotionCampaign.Status.SCHEDULED, campaigns__payment_verified_at__isnull=False),
            ),
            queued_count=Count(
                "campaigns",
                filter=Q(campaigns__status=PromotionCampaign.Status.QUEUED, campaigns__payment_verified_at__isnull=False),
            ),
        ).get(pk=pk)
        inventory.active_count = public_active_campaign_counts_by_inventory(now=timezone.now()).get(inventory.pk, 0)
        return inventory


@method_decorator(never_cache, name="dispatch")
class PromotionManagementAuditView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(tags=["Promotion Management"], summary="Read recent promotion plan and inventory configuration audit events", responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        events = (
            AuditLog.objects.filter(
                action__in=("PROMOTION_PLAN_CONFIGURED", "PROMOTION_INVENTORY_CAPACITY_CHANGED")
            )
            .select_related("actor")
            .order_by("-created_at", "-id")[:50]
        )
        results = []
        for event in events:
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            actor_name = metadata.get("actor_name_snapshot") or (
                (event.actor.get_full_name() or event.actor.email) if event.actor_id else "Former or system actor"
            )
            results.append({
                "id": str(event.pk),
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "actor_name": str(actor_name)[:160],
                "created_at": event.created_at,
                "reason": str(metadata.get("reason") or "Configuration update recorded."),
                "details": metadata,
            })
        return Response({"count": len(results), "results": results})


@method_decorator(never_cache, name="dispatch")
class PromotionCampaignManagementListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, CanReviewPromotionApplications]
    serializer_class = PromotionCampaignManagementSerializer

    def get_queryset(self):
        case_queryset = SupportCase.objects.only("id", "promotion_campaign_id", "reference", "subject", "status", "created_at").order_by("-created_at")
        action_queryset = PromotionCampaignAction.objects.select_related("actor").order_by("-created_at", "-id")[:30]
        queryset = (
            PromotionCampaign.objects.select_related("artisan__user", "inventory", "verified_payment")
            .prefetch_related(
                Prefetch("support_cases", queryset=case_queryset, to_attr="prefetched_support_cases"),
                Prefetch("actions", queryset=action_queryset, to_attr="prefetched_campaign_actions"),
            )
            .order_by("-created_at")
        )
        state = self.request.query_params.get("status")
        promotion_type = self.request.query_params.get("promotion_type")
        if state:
            queryset = queryset.filter(status=state)
        if promotion_type:
            queryset = queryset.filter(promotion_type=promotion_type)
        return queryset

    @extend_schema(tags=["Promotion Management"], summary="Authorized campaign operations list", responses={200: PromotionCampaignManagementSerializer(many=True)})
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


@method_decorator(never_cache, name="dispatch")
class PromotionCampaignActionView(APIView):
    permission_classes = [IsAuthenticated, CanReviewPromotionApplications]

    @extend_schema(tags=["Promotion Management"], request=PromotionCampaignActionSerializer, responses={200: PromotionCampaignManagementSerializer, 409: OpenApiResponse(description="Campaign action is not permitted in its current state.")})
    @idempotent_action("promotion.campaign.action", resource_type="PromotionCampaign")
    def post(self, request, pk):
        campaign = get_object_or_404(PromotionCampaign.objects.select_related("artisan__user", "inventory", "verified_payment"), pk=pk)
        serializer = PromotionCampaignActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["action"] in {"RESTORE", "REVOKE"} and not user_has_support_role(
            request.user,
            {SUPPORT_SUPERVISOR, SUPPORT_ADMIN},
        ):
            raise PermissionDenied("Only a support supervisor, support administrator or platform superuser may restore or revoke a campaign.")
        try:
            campaign, _audit, _refund = pause_or_restore_or_revoke_campaign(
                campaign.id,
                actor=request.user,
                action=serializer.validated_data["action"],
                reason=serializer.validated_data["reason"],
                evidence_summary=serializer.validated_data.get("evidence_summary", ""),
                support_case_id=serializer.validated_data["support_case_id"],
                confirmed_violation=serializer.validated_data.get("confirmed_violation", False),
            )
        except ValidationError:
            raise
        return Response(PromotionCampaignManagementSerializer(campaign).data)
