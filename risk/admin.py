from django import forms
from django.contrib import admin

from .models import (
    CustomerCancellationEvent,
    CustomerNetworkBlock,
    CustomerNetworkObservation,
    CustomerPaymentCancellationEvent,
    CustomerRiskAdjustment,
    CustomerRiskProfile,
)
from .services import risk_percentage


class CustomerRiskProfileForm(forms.ModelForm):
    """Admin correction of a risk score, with the reason the audit requires.

    The only field an administrator may type into is the manual adjustment; the
    derived figures stay read-only so an adjustment can never be faked.
    """

    adjustment_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text=(
            "Required whenever the manual adjustment changes. It is written to "
            "the audit trail and shown to the customer."
        ),
    )

    class Meta:
        model = CustomerRiskProfile
        fields = ("manual_adjustment_points",)

    def clean(self):
        cleaned = super().clean()
        changed = "manual_adjustment_points" in self.changed_data
        if changed and not (cleaned.get("adjustment_reason") or "").strip():
            self.add_error(
                "adjustment_reason",
                "Give a reason for this adjustment; it is recorded in the audit trail.",
            )
        return cleaned


@admin.register(CustomerRiskProfile)
class CustomerRiskProfileAdmin(admin.ModelAdmin):
    form = CustomerRiskProfileForm
    list_display = (
        "customer",
        "cancellation_points",
        "risk_percentage",
        "event_points",
        "manual_adjustment_points",
        "cancellation_count",
        "weighted_cancellation_rate",
        "risk_level",
        "last_evaluated_at",
    )
    list_filter = ("risk_level",)
    search_fields = (
        "customer__user__email",
        "customer__user__first_name",
        "customer__user__last_name",
    )
    readonly_fields = (
        "customer",
        "event_points",
        "cancellation_points",
        "cancellation_count",
        "eligible_booking_count",
        "weighted_cancellation_rate",
        "risk_level",
        "last_evaluated_at",
        "suspended_at",
        "suspension_reason",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Risk %")
    def risk_percentage(self, obj):
        return f"{risk_percentage(obj.cancellation_points)}%"

    def save_model(self, request, obj, form, change):
        from .services import adjust_customer_risk_points

        reason = (form.cleaned_data.get("adjustment_reason") or "").strip()
        changed = "manual_adjustment_points" in form.changed_data
        if not change or not changed:
            super().save_model(request, obj, form, change)
            return
        target = int(form.cleaned_data["manual_adjustment_points"])
        # Route the change through the audited service so the admin panel and
        # the support endpoint behave identically: history preserved, audit row
        # written, customer notified.
        profile, _adjustment = adjust_customer_risk_points(
            customer=obj.customer,
            actor=request.user,
            action=CustomerRiskAdjustment.Action.SET,
            reason=reason,
            points=max(0, min(5, obj.event_points + target)),
        )
        obj.cancellation_points = profile.cancellation_points
        obj.risk_level = profile.risk_level

    def has_delete_permission(self, request, obj=None):
        # Risk files are evidence; they are corrected, never deleted.
        return False


@admin.register(CustomerPaymentCancellationEvent)
class CustomerPaymentCancellationEventAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "payment_reference",
        "booking",
        "points",
        "initiated_by",
        "reason_code",
        "occurred_at",
    )
    list_filter = ("initiated_by", "points")
    search_fields = (
        "customer__user__email",
        "payment_reference",
        "booking__id",
        "reason_text",
    )
    readonly_fields = (
        "customer",
        "payment",
        "booking",
        "payment_reference",
        "reason_code",
        "reason_text",
        "points",
        "initiated_by",
        "occurred_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CustomerRiskAdjustment)
class CustomerRiskAdjustmentAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "action",
        "previous_points",
        "new_points",
        "delta",
        "actor",
        "support_case",
        "created_at",
    )
    list_filter = ("action",)
    search_fields = (
        "customer__user__email",
        "actor__email",
        "reason",
        "support_case__reference",
    )
    readonly_fields = (
        "customer",
        "action",
        "previous_points",
        "new_points",
        "delta",
        "reason",
        "actor",
        "support_case",
        "payment_event",
        "notify_customer",
        "created_at",
    )

    def has_add_permission(self, request):
        # Adjustments are created by the risk profile form, which writes the
        # audit row through the service layer.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CustomerCancellationEvent)
class CustomerCancellationEventAdmin(admin.ModelAdmin):
    """Booking cancellation history — deliberately never scored."""

    list_display = (
        "customer",
        "booking",
        "reason_code",
        "severity",
        "points",
        "occurred_at",
    )
    list_filter = ("reason_code", "customer_attributable", "requires_review")
    search_fields = ("customer__user__email", "booking__id", "reason_text")
    readonly_fields = (
        "customer",
        "booking",
        "reason_code",
        "reason_text",
        "severity",
        "points",
        "customer_attributable",
        "requires_review",
        "ip_hash",
        "occurred_at",
        "scheduled_time_start",
    )

    def has_add_permission(self, request):
        return False


@admin.register(CustomerNetworkObservation)
class CustomerNetworkObservationAdmin(admin.ModelAdmin):
    list_display = ("customer", "ip_hash", "observation_count", "last_seen_at")
    search_fields = ("customer__user__email", "ip_hash")
    readonly_fields = (
        "customer",
        "ip_hash",
        "observation_type",
        "first_seen_at",
        "last_seen_at",
        "observation_count",
    )


@admin.register(CustomerNetworkBlock)
class CustomerNetworkBlockAdmin(admin.ModelAdmin):
    list_display = (
        "ip_hash",
        "scope",
        "confidence",
        "block_count",
        "blocked_until",
        "reason",
    )
    list_filter = ("scope",)
    search_fields = ("ip_hash", "reason")
    readonly_fields = ("created_at", "updated_at")
