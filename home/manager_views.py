from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import audit_request_action
from core.models import AuditLog
from .manager_analytics import build_manager_overview
from .manager_snapshots import (
    configure_snapshot_schedule,
    get_or_create_snapshot_schedule,
    latest_successful_snapshot,
)
from .models import ManagerSnapshotSchedule
from .permissions import IsPlatformSuperuser


class ManagerSnapshotFrequencySerializer(serializers.Serializer):
    frequency = serializers.ChoiceField(choices=ManagerSnapshotSchedule.Frequency.choices)
    reason = serializers.CharField(required=False, max_length=1000, allow_blank=True, trim_whitespace=True)


def _schedule_payload(schedule):
    events = (
        AuditLog.objects.filter(action="MANAGER_SNAPSHOT_SCHEDULE_CONFIGURED")
        .select_related("actor")
        .order_by("-created_at", "-id")[:20]
    )
    history = []
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        actor_name = metadata.get("actor_name_snapshot") or (
            (event.actor.get_full_name() or event.actor.email) if event.actor_id else "Former or system actor"
        )
        history.append({
            "actor_name": str(actor_name)[:160],
            "created_at": event.created_at,
            "reason": str(metadata.get("reason") or "Authorized snapshot schedule change."),
            "before": metadata.get("before") if isinstance(metadata.get("before"), dict) else {},
            "after": metadata.get("after") if isinstance(metadata.get("after"), dict) else {},
        })
    return {
        "frequency": schedule.frequency,
        "frequency_label": schedule.get_frequency_display(),
        "timezone": schedule.timezone_name,
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
        "last_success_at": schedule.last_success_at,
        "last_error_at": schedule.last_error_at,
        "last_error": schedule.last_error,
        "last_period_key": schedule.last_period_key,
        "updated_by": schedule.updated_by_name_snapshot,
        "updated_at": schedule.updated_at,
        "history": history,
    }


def _private_manager_response(payload):
    response = Response(payload)
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


class ManagerOverviewView(APIView):
    """Site-wide, aggregate-only manager dashboard API."""

    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(
        tags=["Platform management"],
        summary="Get the latest scheduled aggregate site analytics snapshot",
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        schedule = get_or_create_snapshot_schedule()
        snapshot = latest_successful_snapshot()
        if snapshot:
            payload = dict(snapshot.payload or {})
            payload["generated_at"] = snapshot.completed_at
            payload["snapshot"] = {
                "status": snapshot.status,
                "period_key": snapshot.period_key,
                "scheduled_for": snapshot.scheduled_for,
                "generated_at": snapshot.completed_at,
                "frequency": snapshot.frequency,
                "timezone": snapshot.timezone_name,
                "last_error": snapshot.last_error,
            }
        else:
            # Keep the dashboard useful before the first Celery beat/worker run;
            # this fallback is live and deliberately does not masquerade as a saved snapshot.
            payload = build_manager_overview()
            payload["snapshot"] = {
                "status": "AWAITING_FIRST_SNAPSHOT",
                "period_key": "",
                "scheduled_for": schedule.next_run_at,
                "generated_at": payload.get("generated_at"),
                "frequency": schedule.frequency,
                "timezone": schedule.timezone_name,
                "last_error": schedule.last_error,
            }
        payload["snapshot_schedule"] = _schedule_payload(schedule)
        audit_request_action(
            request=request,
            action="MANAGER_ANALYTICS_ACCESSED",
            resource_type="ManagerAnalytics",
            resource_id="site-overview",
            metadata={
                "source": "saved_snapshot" if snapshot else "live_fallback",
                "period_key": str(payload.get("snapshot", {}).get("period_key") or "")[:64],
            },
        )
        return _private_manager_response(payload)


class ManagerSnapshotScheduleView(APIView):
    """Select and inspect the persistent, worker-backed manager snapshot schedule."""

    permission_classes = [IsAuthenticated, IsPlatformSuperuser]

    @extend_schema(
        tags=["Platform management"],
        summary="Get the manager analytics snapshot frequency and run status",
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        return _private_manager_response(_schedule_payload(get_or_create_snapshot_schedule()))

    @extend_schema(
        tags=["Platform management"],
        summary="Change the manager analytics snapshot frequency",
        request=ManagerSnapshotFrequencySerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    def patch(self, request):
        serializer = ManagerSnapshotFrequencySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            schedule, before, after, changed = configure_snapshot_schedule(
                actor=request.user,
                frequency=serializer.validated_data["frequency"],
            )
            if changed:
                reason = serializer.validated_data.get("reason") or "Authorized platform manager updated manager snapshot frequency."
                actor_name = request.user.get_full_name() or request.user.email
                audit_request_action(
                    request=request,
                    action="MANAGER_SNAPSHOT_SCHEDULE_CONFIGURED",
                    resource_type="ManagerSnapshotSchedule",
                    resource_id=schedule.key,
                    metadata={
                        "before": before,
                        "after": after,
                        "reason": reason,
                        "actor_name_snapshot": actor_name[:160],
                    },
                )
        return _private_manager_response(_schedule_payload(schedule))
