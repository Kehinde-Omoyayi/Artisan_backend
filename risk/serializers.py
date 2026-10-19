from rest_framework import serializers

from django.conf import settings

from .models import CustomerRiskProfile



class CustomerCancellationRiskSerializer(serializers.ModelSerializer):
    max_points = serializers.SerializerMethodField()
    rolling_window_days = serializers.SerializerMethodField()
    review_threshold = serializers.SerializerMethodField()
    suspension_threshold = serializers.SerializerMethodField()

    class Meta:
        model = CustomerRiskProfile
        fields = [
            "cancellation_points",
            "max_points",
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
