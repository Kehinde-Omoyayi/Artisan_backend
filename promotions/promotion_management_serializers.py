from datetime import datetime
from decimal import Decimal

from django.urls import reverse, NoReverseMatch
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import PromotionCampaign, PromotionPayment, PromotionPlan, PromotionInventory, PromotionCampaignAction


def _promotion_performance_counts(obj):
    impressions = getattr(obj, "performance_impressions", None)
    visits = getattr(obj, "performance_profile_visits", None)
    if impressions is None or visits is None or not (impressions or visits):
        return None, None
    return int(impressions), int(visits)


class PromotionApplicantActionSummarySerializer(serializers.Serializer):
    action = serializers.CharField()
    status_before = serializers.CharField()
    status_after = serializers.CharField()
    reason = serializers.CharField()
    created_at = serializers.DateTimeField()


class PromotionSupportCaseSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    reference = serializers.CharField()
    subject = serializers.CharField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()


class PromotionCampaignAdminActionSummarySerializer(serializers.Serializer):
    id = serializers.CharField()
    action = serializers.CharField()
    action_label = serializers.CharField()
    status_before = serializers.CharField()
    status_after = serializers.CharField()
    reason = serializers.CharField()
    evidence_summary = serializers.CharField(allow_blank=True)
    support_case_id = serializers.IntegerField(allow_null=True)
    actor_name = serializers.CharField()
    created_at = serializers.DateTimeField()
    metadata = serializers.JSONField()


class PromotionOfferSerializer(serializers.Serializer):
    application_id = serializers.UUIDField()
    promotion_type = serializers.CharField()
    promotion_type_label = serializers.CharField()
    target = serializers.DictField()
    plan_name = serializers.CharField()
    price = serializers.DecimalField(max_digits=12, decimal_places=2)
    price_kobo = serializers.IntegerField()
    currency = serializers.CharField()
    duration_days = serializers.IntegerField()
    terms_text = serializers.CharField(allow_blank=True)
    terms_version = serializers.IntegerField()
    terms_changed = serializers.BooleanField()
    availability = serializers.CharField()
    availability_reason = serializers.CharField(allow_blank=True)
    capacity = serializers.IntegerField()
    paid_campaigns_in_inventory = serializers.IntegerField()
    earliest_start_at = serializers.DateTimeField(allow_null=True)
    estimated_wait = serializers.CharField()
    schedule_note = serializers.CharField()
    quote_token = serializers.CharField()
    quote_expires_in_seconds = serializers.IntegerField()


class PromotionCheckoutSerializer(serializers.Serializer):
    quote_token = serializers.CharField(max_length=4000)
    accepted_terms = serializers.BooleanField()
    accepted_terms_version = serializers.IntegerField(min_value=1)


class PromotionPaymentApplicantSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = PromotionPayment
        fields = ["id", "reference", "amount", "currency", "status", "status_label", "authorization_url", "provider_reference", "verified_at", "expires_at", "failure_reason", "created_at"]
        read_only_fields = fields

    def get_amount(self, obj) -> str:
        return str(Decimal(obj.amount_kobo) / Decimal(100))


class PromotionCampaignApplicantSerializer(serializers.ModelSerializer):
    promotion_type_label = serializers.CharField(source="application.get_promotion_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    target_label = serializers.CharField(source="inventory.target_label", read_only=True)
    price = serializers.SerializerMethodField()
    payment_history = PromotionPaymentApplicantSerializer(source="payments", many=True, read_only=True)
    latest_payment = serializers.SerializerMethodField()
    queue_position = serializers.SerializerMethodField()
    estimated_start_at = serializers.SerializerMethodField()
    actions = serializers.SerializerMethodField()
    impressions = serializers.SerializerMethodField()
    profile_clicks = serializers.SerializerMethodField()
    profile_visits = serializers.SerializerMethodField()
    performance_data_available = serializers.SerializerMethodField()
    profile_visit_rate_percent = serializers.SerializerMethodField()

    class Meta:
        model = PromotionCampaign
        fields = [
            "id", "application", "promotion_type", "promotion_type_label", "target_snapshot", "target_label",
            "plan_name_snapshot", "price", "currency", "duration_days", "terms_snapshot", "status", "status_label",
            "payment_verified_at", "scheduled_start_at", "estimated_start_at", "started_at", "expires_at",
            "paused_at", "remaining_duration_seconds", "ended_at", "last_action_reason", "queue_position",
            "payment_history", "latest_payment", "impressions", "profile_clicks", "profile_visits",
            "performance_data_available", "profile_visit_rate_percent", "enquiries", "actions", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_price(self, obj) -> str:
        return str(Decimal(obj.price_kobo) / Decimal(100))

    def get_impressions(self, obj) -> int | None:
        return _promotion_performance_counts(obj)[0]

    def get_profile_clicks(self, obj) -> int | None:
        return _promotion_performance_counts(obj)[1]

    def get_profile_visits(self, obj) -> int | None:
        return _promotion_performance_counts(obj)[1]

    def get_performance_data_available(self, obj) -> bool:
        return _promotion_performance_counts(obj)[0] is not None

    def get_profile_visit_rate_percent(self, obj) -> float | None:
        impressions, visits = _promotion_performance_counts(obj)
        if impressions is None or visits is None or not impressions:
            return None
        return round((visits / impressions) * 100, 2)

    @extend_schema_field(PromotionPaymentApplicantSerializer(allow_null=True))
    def get_latest_payment(self, obj):
        payment = getattr(obj, "latest_payment_obj", None)
        if payment is None:
            return None
        return PromotionPaymentApplicantSerializer(payment, context=self.context).data

    def get_queue_position(self, obj) -> int | None:
        positions = self.context.get("queue_positions", {})
        return positions.get(str(obj.pk))

    def get_estimated_start_at(self, obj) -> datetime | None:
        if obj.scheduled_start_at:
            return obj.scheduled_start_at
        estimates = self.context.get("estimated_starts", {})
        return estimates.get(str(obj.pk))

    @extend_schema_field(PromotionApplicantActionSummarySerializer(many=True))
    def get_actions(self, obj):
        actions = getattr(obj, "prefetched_campaign_actions", None)
        if actions is None:
            actions = obj.actions.all()[:30]
        return [
            {
                "action": item.action,
                "status_before": item.status_before,
                "status_after": item.status_after,
                "reason": item.reason,
                "created_at": item.created_at,
            }
            for item in actions
        ]


class PromotionHomepageArtisanSerializer(serializers.ModelSerializer):
    campaign_id = serializers.UUIDField(source="id", read_only=True)
    artisan_id = serializers.UUIDField(read_only=True)
    name = serializers.SerializerMethodField()
    business_name = serializers.CharField(source="artisan.business_name", read_only=True)
    description = serializers.CharField(source="artisan.bio", read_only=True)
    promotion_description = serializers.CharField(source="description_snapshot", read_only=True)
    profile_photo_url = serializers.SerializerMethodField()
    average_rating = serializers.DecimalField(source="artisan.average_rating", max_digits=3, decimal_places=2, read_only=True)
    review_count = serializers.SerializerMethodField()
    artisan_url = serializers.SerializerMethodField()
    target = serializers.JSONField(source="target_snapshot", read_only=True)
    services = serializers.JSONField(source="services_snapshot", read_only=True)

    class Meta:
        model = PromotionCampaign
        fields = ["campaign_id", "artisan_id", "name", "business_name", "description", "promotion_description", "profile_photo_url", "average_rating", "review_count", "artisan_url", "promotion_type", "target", "services", "started_at", "expires_at"]
        read_only_fields = fields

    def get_name(self, obj) -> str:
        return obj.artisan.user.get_full_name()

    def get_review_count(self, obj) -> int:
        return obj.artisan.reviews_received.count()

    def get_profile_photo_url(self, obj) -> str:
        photo = obj.artisan.profile_photo
        if not photo:
            return ""
        try:
            url = photo.url
        except (ValueError, OSError):
            return ""
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def get_artisan_url(self, obj) -> str:
        try:
            return reverse("artisan-detail", args=[obj.artisan_id])
        except NoReverseMatch:
            return f"/discover/artisans/{obj.artisan_id}/"


class PromotionPlanAdminSerializer(serializers.ModelSerializer):
    price = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PromotionPlan
        fields = ["id", "promotion_type", "name", "is_enabled", "price", "price_kobo", "currency", "duration_days", "refund_policy", "terms_text", "version", "updated_at", "updated_by", "updated_by_name"]
        read_only_fields = ["id", "currency", "version", "updated_at", "updated_by", "updated_by_name"]

    def get_price(self, obj) -> str:
        return str(Decimal(obj.price_kobo) / Decimal(100))

    def get_updated_by_name(self, obj) -> str:
        if not obj.updated_by_id:
            return ""
        return obj.updated_by.get_full_name() or obj.updated_by.email


class PromotionPlanUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False)
    is_enabled = serializers.BooleanField(required=False)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.00"))
    duration_days = serializers.IntegerField(required=False, min_value=1, max_value=365)
    refund_policy = serializers.ChoiceField(choices=PromotionPlan.RefundPolicy.choices, required=False)
    terms_text = serializers.CharField(required=False, max_length=10000, allow_blank=True)
    reason = serializers.CharField(required=False, max_length=1000, allow_blank=True, trim_whitespace=True)

    def validate(self, attrs):
        instance = self.instance
        price = attrs.get("price", Decimal(instance.price_kobo) / Decimal(100) if instance else Decimal("0"))
        enabled = attrs.get("is_enabled", instance.is_enabled if instance else False)
        terms = attrs.get("terms_text", instance.terms_text if instance else "")
        if price != price.to_integral_value():
            raise serializers.ValidationError({"price": "The current Flutterwave promotion checkout supports whole-NGN amounts only."})
        if enabled and (price <= 0 or len(terms.strip()) < 20):
            raise serializers.ValidationError({"is_enabled": "An enabled plan needs a positive whole-NGN price and at least 20 characters of customer-facing terms."})
        return attrs


class PromotionInventorySerializer(serializers.ModelSerializer):
    active_count = serializers.IntegerField(read_only=True)
    scheduled_count = serializers.IntegerField(read_only=True)
    queued_count = serializers.IntegerField(read_only=True)
    remaining_capacity = serializers.SerializerMethodField()

    class Meta:
        model = PromotionInventory
        fields = ["id", "promotion_type", "target_key", "target_label", "capacity", "active_count", "scheduled_count", "queued_count", "remaining_capacity", "updated_at"]
        read_only_fields = ["id", "promotion_type", "target_key", "target_label", "active_count", "scheduled_count", "queued_count", "remaining_capacity", "updated_at"]

    def get_remaining_capacity(self, obj) -> int:
        return max(int(obj.capacity) - int(getattr(obj, "active_count", 0)), 0)


class PromotionInventoryCapacitySerializer(serializers.Serializer):
    capacity = serializers.IntegerField(min_value=0, max_value=1000)
    reason = serializers.CharField(required=False, max_length=1000, allow_blank=True, trim_whitespace=True)


class PromotionCampaignManagementSerializer(serializers.ModelSerializer):
    artisan_name = serializers.SerializerMethodField()
    artisan_email = serializers.EmailField(source="artisan.user.email", read_only=True)
    target_label = serializers.CharField(source="inventory.target_label", read_only=True)
    payment_reference = serializers.CharField(source="verified_payment.reference", read_only=True, allow_null=True)
    price = serializers.SerializerMethodField()
    support_cases = serializers.SerializerMethodField()
    actions = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    impressions = serializers.SerializerMethodField()
    profile_clicks = serializers.SerializerMethodField()
    profile_visits = serializers.SerializerMethodField()
    performance_data_available = serializers.SerializerMethodField()
    profile_visit_rate_percent = serializers.SerializerMethodField()

    class Meta:
        model = PromotionCampaign
        fields = ["id", "application", "artisan", "artisan_name", "artisan_email", "promotion_type", "target_label", "plan_name_snapshot", "price", "currency", "status", "status_label", "payment_reference", "payment_verified_at", "scheduled_start_at", "started_at", "expires_at", "paused_at", "last_action_reason", "impressions", "profile_clicks", "profile_visits", "performance_data_available", "profile_visit_rate_percent", "enquiries", "support_cases", "actions", "created_at"]
        read_only_fields = fields

    @extend_schema_field(PromotionCampaignAdminActionSummarySerializer(many=True))
    def get_actions(self, obj):
        actions = getattr(obj, "prefetched_campaign_actions", None)
        if actions is None:
            actions = obj.actions.select_related("actor").order_by("-created_at", "-id")[:30]
        return [
            {
                "id": str(action.pk),
                "action": action.action,
                "action_label": action.get_action_display(),
                "status_before": action.status_before,
                "status_after": action.status_after,
                "reason": action.reason,
                "evidence_summary": action.evidence_summary,
                "support_case_id": action.support_case_id,
                "actor_name": action.actor_name_snapshot or (
                    (action.actor.get_full_name() or action.actor.email) if action.actor_id else "System"
                ),
                "created_at": action.created_at,
                "metadata": action.metadata,
            }
            for action in actions
        ]

    @extend_schema_field(PromotionSupportCaseSummarySerializer(many=True))
    def get_support_cases(self, obj):
        cases = getattr(obj, "prefetched_support_cases", None)
        if cases is None:
            cases = obj.support_cases.all().order_by("-created_at")[:30]
        return [
            {"id": case.pk, "reference": case.reference, "subject": case.subject, "status": case.status, "created_at": case.created_at}
            for case in cases
        ]

    def get_artisan_name(self, obj) -> str:
        return obj.artisan.user.get_full_name() or obj.artisan.user.email

    def get_price(self, obj) -> str:
        return str(Decimal(obj.price_kobo) / Decimal(100))

    def get_impressions(self, obj) -> int | None:
        return _promotion_performance_counts(obj)[0]

    def get_profile_clicks(self, obj) -> int | None:
        return _promotion_performance_counts(obj)[1]

    def get_profile_visits(self, obj) -> int | None:
        return _promotion_performance_counts(obj)[1]

    def get_performance_data_available(self, obj) -> bool:
        return _promotion_performance_counts(obj)[0] is not None

    def get_profile_visit_rate_percent(self, obj) -> float | None:
        impressions, visits = _promotion_performance_counts(obj)
        if impressions is None or visits is None or not impressions:
            return None
        return round((visits / impressions) * 100, 2)


class PromotionCampaignActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["PAUSE", "SUSPEND", "RESTORE", "REVOKE"])
    support_case_id = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(min_length=10, max_length=2000, trim_whitespace=True)
    evidence_summary = serializers.CharField(required=False, max_length=5000, allow_blank=True)
    confirmed_violation = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["action"] == "REVOKE" and attrs.get("confirmed_violation") and len(attrs.get("evidence_summary", "").strip()) < 20:
            raise serializers.ValidationError({"evidence_summary": "Summarize the reviewed evidence; a complaint alone is not proof."})
        return attrs
