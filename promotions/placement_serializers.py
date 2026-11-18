from django.urls import NoReverseMatch, reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from home.models import ArtisanService
from services.serializers import ArtisanServiceSerializer
from .models import ArtisanMarketPick, PromotionCampaignPerformanceEvent


class PromotionPerformanceEventSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=4000)
    visitor_id = serializers.CharField(min_length=24, max_length=180)
    event_type = serializers.ChoiceField(choices=PromotionCampaignPerformanceEvent.EventType.choices)


class MarketPickPublicSerializer(serializers.ModelSerializer):
    artisan_id = serializers.UUIDField(read_only=True)
    name = serializers.SerializerMethodField()
    business_name = serializers.CharField(source="artisan.business_name", read_only=True)
    description = serializers.CharField(source="artisan.bio", read_only=True)
    profile_photo_url = serializers.SerializerMethodField()
    average_rating = serializers.DecimalField(source="artisan.average_rating", max_digits=3, decimal_places=2, read_only=True)
    review_count = serializers.IntegerField(source="artisan.review_count", read_only=True)
    completed_booking_count = serializers.IntegerField(source="artisan.completed_booking_count", read_only=True)
    main_service = serializers.SerializerMethodField()
    services = serializers.SerializerMethodField()
    artisan_url = serializers.SerializerMethodField()
    placement_type = serializers.SerializerMethodField()

    class Meta:
        model = ArtisanMarketPick
        fields = [
            "id", "artisan_id", "name", "business_name", "description", "profile_photo_url",
            "average_rating", "review_count", "completed_booking_count", "main_service", "services",
            "artisan_url", "position", "placement_type",
        ]
        read_only_fields = fields

    def get_name(self, obj) -> str:
        user = obj.artisan.user
        return user.get_full_name() or obj.artisan.business_name

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

    def _services(self, obj):
        rows = getattr(obj.artisan, "market_pick_services", None)
        if rows is None:
            rows = obj.artisan.artisan_services.filter(service__is_active=True).select_related("service__category", "location").order_by("service__name", "pk")
        return list(rows)[:3]

    def get_main_service(self, obj) -> str:
        services = self._services(obj)
        if not services:
            return ""
        return services[0].service.name

    @extend_schema_field(ArtisanServiceSerializer(many=True))
    def get_services(self, obj):
        return ArtisanServiceSerializer(self._services(obj), many=True, context=self.context).data

    def get_artisan_url(self, obj) -> str:
        try:
            return reverse("artisan-detail", args=[obj.artisan_id])
        except NoReverseMatch:
            return f"/discover/artisans/{obj.artisan_id}/"

    def get_placement_type(self, obj) -> str:
        return "ARTISAN_MARKET_PICK"


class MarketPickCandidateSerializer(serializers.Serializer):
    artisan_id = serializers.UUIDField(source="id", read_only=True)
    name = serializers.SerializerMethodField()
    business_name = serializers.CharField(read_only=True)
    registered_location = serializers.SerializerMethodField()
    main_service = serializers.SerializerMethodField()
    average_rating = serializers.DecimalField(max_digits=3, decimal_places=2, read_only=True)
    review_count = serializers.IntegerField(read_only=True)
    completed_booking_count = serializers.IntegerField(read_only=True)
    has_portfolio = serializers.SerializerMethodField()

    def get_name(self, obj) -> str:
        return obj.user.get_full_name()

    def get_registered_location(self, obj) -> str:
        return ", ".join(value for value in (obj.area, obj.state, obj.country) if value)

    def get_main_service(self, obj) -> str:
        services = getattr(obj, "market_pick_services", None)
        if services is None:
            services = obj.artisan_services.filter(service__is_active=True).select_related("service").order_by("service__name", "pk")
        service = next(iter(services), None)
        return service.service.name if service else ""

    def get_has_portfolio(self, obj) -> bool:
        return bool(obj.portfolio_url)


class MarketPickManagementSerializer(serializers.ModelSerializer):
    artisan_id = serializers.UUIDField(read_only=True)
    name = serializers.SerializerMethodField()
    business_name = serializers.CharField(source="artisan.business_name", read_only=True)
    main_service = serializers.SerializerMethodField()
    average_rating = serializers.DecimalField(source="artisan.average_rating", max_digits=3, decimal_places=2, read_only=True)
    review_count = serializers.IntegerField(source="artisan_review_count", read_only=True)
    completed_booking_count = serializers.IntegerField(source="artisan_completed_booking_count", read_only=True)
    has_portfolio = serializers.SerializerMethodField()
    currently_eligible = serializers.BooleanField(read_only=True, default=False)

    class Meta:
        model = ArtisanMarketPick
        fields = [
            "id", "artisan_id", "name", "business_name", "main_service", "position", "selection_note",
            "is_active", "selected_at", "average_rating", "review_count", "completed_booking_count", "has_portfolio", "currently_eligible",
        ]
        read_only_fields = fields

    def get_name(self, obj) -> str:
        return obj.artisan.user.get_full_name()

    def get_main_service(self, obj) -> str:
        services = getattr(obj.artisan, "market_pick_services", None)
        if services is None:
            services = obj.artisan.artisan_services.filter(service__is_active=True).select_related("service").order_by("service__name", "pk")
        service = next(iter(services), None)
        return service.service.name if service else ""

    def get_has_portfolio(self, obj) -> bool:
        return bool(obj.artisan.portfolio_url)


class MarketPickManagementActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["ADD", "UPDATE", "REMOVE"])
    artisan_id = serializers.UUIDField()
    position = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)
    selection_note = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("selection_note", "").strip():
            raise serializers.ValidationError({"selection_note": "Record the editorial selection or removal reason for the audit trail."})
        return attrs
