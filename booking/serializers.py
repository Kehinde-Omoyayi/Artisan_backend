from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from rest_framework import serializers

from home.models import ArtisanProfile
from services.models import ServiceRequest
from risk.policy import CUSTOMER_CANCEL_REASON_CHOICES
from support.validation import normalize_support_description

from .models import Booking, BookingStatusHistory

class BookingSerializer(serializers.ModelSerializer):
    request_id = serializers.PrimaryKeyRelatedField(source="request", queryset=ServiceRequest.objects.all())
    customer_id = serializers.UUIDField(source="customer.id", read_only=True)
    artisan_id = serializers.PrimaryKeyRelatedField(source="artisan", queryset=ArtisanProfile.objects.all())
    conversation_id = serializers.SerializerMethodField()
    viewer_role = serializers.SerializerMethodField()
    duration_label = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()
    service_name = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    artisan_name = serializers.SerializerMethodField()

    def get_duration_label(self, obj) -> str:
        from .models import duration_label

        return duration_label(obj.service_duration_value, obj.service_duration_unit)

    def get_service_name(self, obj) -> str:
        category = getattr(obj.request, "category", None)
        return getattr(category, "name", "") or ""

    def get_customer_name(self, obj) -> str:
        user = getattr(obj.customer, "user", None)
        if user is None:
            return ""
        return (f"{user.first_name} {user.last_name}".strip()) or user.email

    def get_artisan_name(self, obj) -> str:
        profile = obj.artisan
        business = getattr(profile, "business_name", "") or ""
        user = getattr(profile, "user", None)
        personal = f"{user.first_name} {user.last_name}".strip() if user else ""
        return business or personal

    def get_available_actions(self, obj):
        """Server-derived action list for the requesting viewer.

        The interface renders buttons from this list only, so an action that the
        API would reject is never offered in the first place.
        """
        from .actions import available_actions

        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return []
        obj._conversation_id = self.get_conversation_id(obj)
        return available_actions(obj, request.user)

    class Meta:
        model = Booking
        fields = ["id", "request_id", "customer_id", "artisan_id", "conversation_id", "viewer_role", "scheduled_time_start", "scheduled_time_end", "price_agreed", "status", "source", "service_duration_value", "service_duration_unit", "duration_label", "available_actions", "service_name", "customer_name", "artisan_name", "actual_started_at", "countdown_ends_at", "artisan_completed_at", "customer_completed_at", "payout_eligible_at", "finalized_at", "created_at", "updated_at"]
        read_only_fields = ["id", "customer_id", "conversation_id", "viewer_role", "status", "source", "service_duration_value", "service_duration_unit", "duration_label", "available_actions", "service_name", "customer_name", "artisan_name", "actual_started_at", "countdown_ends_at", "artisan_completed_at", "customer_completed_at", "payout_eligible_at", "finalized_at", "created_at", "updated_at"]

    def get_conversation_id(self, obj) -> str | None:
        if obj.request.requester_id != obj.customer.user_id:
            return None
        try:
            conversation = obj.request.conversation
        except ObjectDoesNotExist:
            return None
        if (
            conversation.conversation_type != "SERVICE"
            or conversation.service_request_id != obj.request_id
            or conversation.customer_id != obj.customer.user_id
            or conversation.artisan_id != obj.artisan_id
        ):
            return None
        return str(conversation.pk)

    def get_viewer_role(self, obj) -> str | None:
        request = self.context.get("request")
        if not request:
            return None
        if obj.customer.user_id == request.user.id:
            return "customer"
        if obj.artisan.user_id == request.user.id:
            return "artisan"
        return None

    def validate(self, attrs):
        start = attrs.get("scheduled_time_start")
        end = attrs.get("scheduled_time_end")
        price = attrs.get("price_agreed")

        if start and timezone.is_naive(start):
            raise serializers.ValidationError({"scheduled_time_start": "A timezone-aware datetime is required."})

        if end and timezone.is_naive(end):
            raise serializers.ValidationError({"scheduled_time_end": "A timezone-aware datetime is required."})

        if start and start < timezone.now():
            raise serializers.ValidationError({"scheduled_time_start": "Booking start time cannot be in the past."})

        if start and end and end <= start:
            raise serializers.ValidationError({"scheduled_time_end": "End time must be later than start time."})

        if price is None:
            raise serializers.ValidationError({"price_agreed": "Agreed price is required before creating a booking."})

        if price <= 0:
            raise serializers.ValidationError({"price_agreed": "Agreed price must be greater than zero."})

        return attrs

class BookingActionReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Reason cannot be empty.")
        return value


class BookingDisputeReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)

    def validate_reason(self, value):
        try:
            return normalize_support_description(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class BookingCancellationSerializer(serializers.Serializer):
    reason_code = serializers.ChoiceField(choices=CUSTOMER_CANCEL_REASON_CHOICES, required=False, default="OTHER")
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Reason cannot be empty.")
        return value


class BookingCompletionSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, trim_whitespace=True)

class BookingStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_user_id = serializers.UUIDField(source="changed_by_user.id", read_only=True)

    class Meta:
        model = BookingStatusHistory
        fields = ["id", "status", "changed_by", "changed_by_user_id", "notes", "created_at"]
        read_only_fields = fields
