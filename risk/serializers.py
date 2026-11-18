from rest_framework import serializers

from django.conf import settings

from .models import (
    CustomerPaymentCancellationEvent,
    CustomerRiskAdjustment,
    CustomerRiskProfile,
)
from .services import risk_percentage


class CustomerPaymentCancellationEventSerializer(serializers.ModelSerializer):
    """One qualifying payment cancellation, with the references support needs."""

    booking_reference = serializers.SerializerMethodField()
    points_label = serializers.SerializerMethodField()

    class Meta:
        model = CustomerPaymentCancellationEvent
        fields = [
            "id",
            "payment_reference",
            "booking_reference",
            "reason_code",
            "reason_text",
            "points",
            "points_label",
            "initiated_by",
            "occurred_at",
        ]
        read_only_fields = fields

    def get_booking_reference(self, obj) -> str:
        return str(obj.booking_id) if obj.booking_id else ""

    def get_points_label(self, obj) -> str:
        return f"{obj.points}/5"


class CustomerRiskAdjustmentSerializer(serializers.ModelSerializer):
    """An audited manual change, readable by the customer and by support."""

    actor_email = serializers.SerializerMethodField()
    action_label = serializers.SerializerMethodField()
    support_case_reference = serializers.SerializerMethodField()

    class Meta:
        model = CustomerRiskAdjustment
        fields = [
            "id",
            "action",
            "action_label",
            "previous_points",
            "new_points",
            "delta",
            "reason",
            "actor_email",
            "support_case_reference",
            "created_at",
        ]
        read_only_fields = fields

    def get_actor_email(self, obj) -> str:
        return getattr(obj.actor, "email", "") or ""

    def get_action_label(self, obj) -> str:
        return obj.get_action_display()

    def get_support_case_reference(self, obj) -> str:
        return getattr(obj.support_case, "reference", "") or ""


class CustomerCancellationRiskSerializer(serializers.ModelSerializer):
    """Risk status shown to the customer and to support.

    The score is points out of five; the percentage is that same score on a
    0-100 scale (1/5 = 20%, 5/5 = 100%). Reaching the top of the scale raises a
    review and never blocks the account.
    """

    max_points = serializers.SerializerMethodField()
    rolling_window_days = serializers.SerializerMethodField()
    review_threshold = serializers.SerializerMethodField()
    suspension_threshold = serializers.SerializerMethodField()
    risk_percentage = serializers.SerializerMethodField()
    score_label = serializers.SerializerMethodField()
    payment_cancellations = serializers.SerializerMethodField()
    adjustments = serializers.SerializerMethodField()

    class Meta:
        model = CustomerRiskProfile
        fields = [
            "cancellation_points",
            "max_points",
            "score_label",
            "risk_percentage",
            "cancellation_count",
            "eligible_booking_count",
            "weighted_cancellation_rate",
            "risk_level",
            "rolling_window_days",
            "review_threshold",
            "suspension_threshold",
            "last_evaluated_at",
            "suspended_at",
            "suspension_reason",
            "payment_cancellations",
            "adjustments",
        ]
        read_only_fields = fields

    def get_max_points(self, obj) -> int:
        return getattr(settings, "CANCELLATION_SUSPENSION_SCORE", 5)

    def get_rolling_window_days(self, obj) -> int:
        return getattr(settings, "CANCELLATION_RISK_WINDOW_DAYS", 90)

    def get_review_threshold(self, obj) -> int:
        return getattr(settings, "CANCELLATION_REVIEW_SCORE", 4)

    def get_suspension_threshold(self, obj) -> int:
        return getattr(settings, "CANCELLATION_SUSPENSION_SCORE", 5)

    def get_risk_percentage(self, obj) -> int:
        return risk_percentage(obj.cancellation_points)

    def get_score_label(self, obj) -> str:
        return f"{obj.cancellation_points}/{self.get_max_points(obj)}"

    def get_payment_cancellations(self, obj):
        from datetime import timedelta

        from django.utils import timezone

        cutoff = timezone.now() - timedelta(
            days=self.get_rolling_window_days(obj)
        )
        events = (
            CustomerPaymentCancellationEvent.objects.filter(
                customer=obj.customer, occurred_at__gte=cutoff
            )
            .select_related("booking")
            .order_by("-occurred_at")[:50]
        )
        return CustomerPaymentCancellationEventSerializer(events, many=True).data

    def get_adjustments(self, obj):
        adjustments = (
            CustomerRiskAdjustment.objects.filter(customer=obj.customer)
            .select_related("actor", "support_case")
            .order_by("-created_at")[:50]
        )
        return CustomerRiskAdjustmentSerializer(adjustments, many=True).data


class SupportCustomerRiskAdjustSerializer(serializers.Serializer):
    """The only write surface support has on a customer's risk score."""

    ACTION_CHOICES = list(CustomerRiskAdjustment.Action.choices)

    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    points = serializers.IntegerField(min_value=0, max_value=5, required=False)
    reason = serializers.CharField(min_length=5, max_length=1000)
    case_id = serializers.IntegerField(required=False)
    notify_customer = serializers.BooleanField(default=True, required=False)

    def validate(self, attrs):
        action = attrs.get("action")
        if action == CustomerRiskAdjustment.Action.SET and attrs.get("points") is None:
            raise serializers.ValidationError(
                {"points": "A target score between 0 and 5 is required for SET."}
            )
        if action != CustomerRiskAdjustment.Action.SET:
            attrs.pop("points", None)
        return attrs


class SupportCustomerRiskSummarySerializer(serializers.ModelSerializer):
    """Compact row for the support customer search."""

    email = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    risk_percentage = serializers.SerializerMethodField()
    account_type = serializers.SerializerMethodField()

    class Meta:
        model = CustomerRiskProfile
        fields = [
            "id",
            "customer",
            "email",
            "name",
            "account_type",
            "cancellation_points",
            "event_points",
            "manual_adjustment_points",
            "risk_percentage",
            "cancellation_count",
            "risk_level",
            "last_evaluated_at",
        ]
        read_only_fields = fields

    def get_email(self, obj) -> str:
        return getattr(obj.customer.user, "email", "") or ""

    def get_name(self, obj) -> str:
        user = getattr(obj.customer, "user", None)
        return user.get_full_name() if user else ""

    def get_risk_percentage(self, obj) -> int:
        return risk_percentage(obj.cancellation_points)

    def get_account_type(self, obj) -> str:
        user = getattr(obj.customer, "user", None)
        if user is None:
            return "customer"
        return "customer" if not hasattr(user, "artisan_profile") else "customer & artisan"
