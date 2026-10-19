from django.contrib import admin

from .models import (
    CustomerCancellationEvent,
    CustomerNetworkBlock,
    CustomerNetworkObservation,
    CustomerRiskProfile,
)


@admin.register(CustomerRiskProfile)
class CustomerRiskProfileAdmin(admin.ModelAdmin):
    list_display = ["customer", "cancellation_points", "weighted_cancellation_rate", "risk_level", "last_evaluated_at"]
    list_filter = ["risk_level"]
    search_fields = ["customer__user__email", "customer__user__first_name", "customer__user__last_name"]
    readonly_fields = [
        "customer", "cancellation_points", "cancellation_count",
        "eligible_booking_count", "weighted_cancellation_rate", "risk_level",
        "last_evaluated_at", "suspended_at", "suspension_reason",
        "created_at", "updated_at",
    ]


@admin.register(CustomerCancellationEvent)
class CustomerCancellationEventAdmin(admin.ModelAdmin):
    list_display = ["customer", "booking", "reason_code", "severity", "points", "occurred_at"]
    list_filter = ["reason_code", "customer_attributable", "requires_review"]
    search_fields = ["customer__user__email", "booking__id", "reason_text"]
    readonly_fields = [
        "customer", "booking", "reason_code", "reason_text", "severity", "points",
        "customer_attributable", "requires_review", "ip_hash", "occurred_at",
        "scheduled_time_start",
    ]


@admin.register(CustomerNetworkObservation)
class CustomerNetworkObservationAdmin(admin.ModelAdmin):
    list_display = ["customer", "ip_hash", "observation_count", "last_seen_at"]
    search_fields = ["customer__user__email", "ip_hash"]
    readonly_fields = [
        "customer", "ip_hash", "observation_type", "first_seen_at",
        "last_seen_at", "observation_count",
    ]


@admin.register(CustomerNetworkBlock)
class CustomerNetworkBlockAdmin(admin.ModelAdmin):
    list_display = ["ip_hash", "scope", "confidence", "block_count", "blocked_until", "reason"]
    list_filter = ["scope"]
    search_fields = ["ip_hash", "reason"]
    readonly_fields = ["created_at", "updated_at"]
