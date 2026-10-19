import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from home.models import ArtisanProfile, CustomerProfile
from home.profile_links import (
    SOCIAL_MEDIA_PLATFORM_CHOICES,
    validate_external_profile_url,
    validate_social_media_url,
)

from services.identity_names import profile_name_matches_identity
from services.image_utils import optimize_profile_photo
from services.kyc_crypto import decrypt_identity_name
from services.models import ArtisanVerification
from services.serializers import ArtisanServiceSerializer

logger = logging.getLogger(__name__)


def _delete_unshared_profile_photo(storage, name, using=None):
    """Delete a photo only when no artisan or customer profile references its name."""
    if not storage or not name:
        return False
    for model in (ArtisanProfile, CustomerProfile):
        profiles = model.objects.all()
        if using:
            profiles = profiles.using(using)
        if profiles.filter(profile_photo=name).exists():
            return False
    storage.delete(name)
    return True


def _schedule_unshared_profile_photo_cleanup(storage, name, using=None):
    """Delete an obsolete upload only after commit and only when it is unreferenced."""
    if not name:
        return

    def cleanup():
        try:
            # optimize_profile_photo stores one WebP; no thumbnails are generated.
            _delete_unshared_profile_photo(storage, name, using=using)
        except Exception:
            # A failed best-effort cleanup must not turn a committed profile update into a 500.
            logger.warning("Could not remove an obsolete profile photo after commit.", exc_info=True)

    transaction.on_commit(cleanup, using=using)


class ArtisanProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name")
    last_name = serializers.CharField(source="user.last_name")
    phone_number = serializers.CharField(source="user.phone_number", allow_blank=True, allow_null=True, required=False)
    profile_photo = serializers.ImageField(required=False, allow_null=True)
    social_media_1_platform = serializers.ChoiceField(
        choices=SOCIAL_MEDIA_PLATFORM_CHOICES, required=False, allow_blank=True
    )
    social_media_1_platform_label = serializers.CharField(
        source="get_social_media_1_platform_display", read_only=True
    )
    social_media_1_url = serializers.URLField(
        required=False, allow_blank=True, max_length=2048
    )
    social_media_2_platform = serializers.ChoiceField(
        choices=SOCIAL_MEDIA_PLATFORM_CHOICES, required=False, allow_blank=True
    )
    social_media_2_platform_label = serializers.CharField(
        source="get_social_media_2_platform_display", read_only=True
    )
    social_media_2_url = serializers.URLField(
        required=False, allow_blank=True, max_length=2048
    )
    portfolio_url = serializers.URLField(
        required=False, allow_blank=True, max_length=2048
    )
    average_rating = serializers.DecimalField(max_digits=3, decimal_places=2, read_only=True)
    completion_rate = serializers.FloatField(read_only=True)
    cancellation_rate = serializers.FloatField(read_only=True)
    profile_status = serializers.CharField(read_only=True)

    class Meta:
        model = ArtisanProfile
        fields = [
            "id","first_name", "last_name", "phone_number", "email", "profile_photo",
            "business_name", "bio", "years_experience", "country", "state", "area",
            "social_media_1_platform", "social_media_1_platform_label", "social_media_1_url",
            "social_media_2_platform", "social_media_2_platform_label", "social_media_2_url", "portfolio_url",
            "average_rating", "completion_rate", "cancellation_rate", "profile_status",
        ]
        read_only_fields = ["id","email", "average_rating", "completion_rate", "cancellation_rate", "profile_status"]

    def validate_phone_number(self, value):
        from home.serializers import normalize_phone
        return normalize_phone(value)

    def validate(self, attrs):
        errors = {}
        for index in (1, 2):
            platform_field = f"social_media_{index}_platform"
            url_field = f"social_media_{index}_url"
            platform = attrs.get(platform_field, getattr(self.instance, platform_field, "") if self.instance else "")
            url = attrs.get(url_field, getattr(self.instance, url_field, "") if self.instance else "")
            if bool(platform) != bool(url):
                errors[url_field] = "Choose both a platform and its profile URL, or leave both blank."
            elif url:
                try:
                    validate_social_media_url(url, platform)
                except DjangoValidationError as exc:
                    errors[url_field] = exc.messages
        if "portfolio_url" in attrs and attrs["portfolio_url"]:
            try:
                validate_external_profile_url(attrs["portfolio_url"])
            except DjangoValidationError as exc:
                errors["portfolio_url"] = exc.messages
        if errors:
            raise serializers.ValidationError(errors)
        if not self.instance:
            return attrs
        user_data = attrs.get("user", {})
        if not isinstance(user_data, dict):
            user_data = {}
        proposed_first = user_data.get("first_name", self.instance.user.first_name)
        proposed_last = user_data.get("last_name", self.instance.user.last_name)
        names_changed = (
            proposed_first != self.instance.user.first_name
            or proposed_last != self.instance.user.last_name
        )
        if not names_changed:
            return attrs

        verification = ArtisanVerification.objects.filter(
            artisan_id=self.instance.pk,
            verification_status="VERIFIED",
        ).first()
        if verification and verification.verified_identity_name_encrypted:
            try:
                identity_name = decrypt_identity_name(
                    verification.verified_identity_name_encrypted
                )
            except ValueError:
                raise serializers.ValidationError({
                    "first_name": "Complete identity verification again before changing the name on a verified profile."
                }) from None
            if not profile_name_matches_identity(identity_name, proposed_first, proposed_last):
                raise serializers.ValidationError({
                    "first_name": "Your profile first and last name must match your verified identity. Correct the profile name or complete identity verification again."
                })
        return attrs

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        profile_photo_supplied = "profile_photo" in validated_data
        profile_photo = validated_data.pop("profile_photo", None)
        old_photo = instance.profile_photo
        old_photo_name = old_photo.name if old_photo else ""
        photo_storage = old_photo.storage if old_photo else instance.profile_photo.storage
        using = instance._state.db

        try:
            with transaction.atomic(using=using):
                user = instance.user
                for field in ["first_name", "last_name", "phone_number"]:
                    if field in user_data:
                        setattr(user, field, user_data[field])
                user.save(update_fields=["first_name", "last_name", "phone_number", "updated_at"])

                for field, value in validated_data.items():
                    setattr(instance, field, value)

                if profile_photo_supplied:
                    if profile_photo is None:
                        # Keep the old file intact until the profile field change commits.
                        instance.profile_photo = None
                    else:
                        optimized = optimize_profile_photo(profile_photo)
                        # A fresh server-generated name prevents overwrite-on-upload storage
                        # backends from replacing the old file before the DB reference commits.
                        optimized.name = f"{uuid4().hex}.webp"
                        instance.profile_photo.save(optimized.name, optimized, save=False)

                instance.save()
                new_photo_name = instance.profile_photo.name if instance.profile_photo else ""
                if profile_photo_supplied and old_photo_name and old_photo_name != new_photo_name:
                    _schedule_unshared_profile_photo_cleanup(
                        old_photo.storage,
                        old_photo_name,
                        using=using,
                    )
        except Exception:
            # File storage is not transactional. If the new upload was staged but the
            # database update failed, remove only that unreferenced new file and restore
            # the in-memory FieldFile to the old database reference.
            staged_photo_name = instance.profile_photo.name if instance.profile_photo else ""
            if profile_photo_supplied and profile_photo is not None and staged_photo_name != old_photo_name:
                try:
                    _delete_unshared_profile_photo(photo_storage, staged_photo_name, using=using)
                except Exception:
                    logger.warning("Could not remove a staged profile photo after rollback.", exc_info=True)
            instance.profile_photo = old_photo_name or None
            raise

        return instance

class ArtisanProfileTextUpdateSerializer(ArtisanProfileSerializer):
    """OpenAPI request shape for updates sent without a multipart file upload."""

    profile_photo = None

    class Meta(ArtisanProfileSerializer.Meta):
        fields = [field for field in ArtisanProfileSerializer.Meta.fields if field != "profile_photo"]


class CustomerProfileSerializer(serializers.ModelSerializer):
    cancellation_rate = serializers.SerializerMethodField()
    cancellation_risk_score = serializers.SerializerMethodField()
    cancellation_risk_max = serializers.SerializerMethodField()
    cancellation_risk_level = serializers.SerializerMethodField()
    first_name = serializers.CharField(source="user.first_name")
    last_name = serializers.CharField(source="user.last_name")
    email = serializers.EmailField(source="user.email", read_only=True)
    phone_number = serializers.CharField(source="user.phone_number", allow_blank=True, allow_null=True, required=False)
    profile_photo = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = CustomerProfile
        fields = [
            "id", "first_name", "last_name", "email", "phone_number", "profile_photo",
            "date_of_birth", "country", "state", "area", "gender",
            "cancellation_rate", "cancellation_risk_score",
            "cancellation_risk_max", "cancellation_risk_level",
        ]
        read_only_fields = [
            "id","email", "cancellation_rate", "cancellation_risk_score",
            "cancellation_risk_max", "cancellation_risk_level",
        ]

    def _risk_profile(self, obj):
        # Cache the computed profile for the lifetime of this serializer instance.
        # The risk profile is deterministic for the current request and should not
        # be recomputed once per read-only field.
        if not hasattr(self, "_risk_profile_cache"):
            from risk.services import get_customer_risk_profile
            self._risk_profile_cache = get_customer_risk_profile(obj)
        return self._risk_profile_cache

    def get_cancellation_rate(self, obj) -> float:
        return float(self._risk_profile(obj).weighted_cancellation_rate)

    def get_cancellation_risk_score(self, obj) -> int:
        return self._risk_profile(obj).cancellation_points

    def get_cancellation_risk_max(self, obj) -> int:
        from django.conf import settings
        return getattr(settings, "CANCELLATION_SUSPENSION_SCORE", 5)

    def get_cancellation_risk_level(self, obj) -> str:
        return self._risk_profile(obj).risk_level

    def validate_phone_number(self, value):
        from home.serializers import normalize_phone
        return normalize_phone(value)

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        photo_supplied = "profile_photo" in validated_data
        photo_upload = validated_data.pop("profile_photo", None)
        old_photo = instance.profile_photo
        old_photo_name = old_photo.name if old_photo else ""
        storage = instance.profile_photo.storage
        using = instance._state.db

        try:
            with transaction.atomic(using=using):
                user = instance.user
                for field in ["first_name", "last_name", "phone_number"]:
                    if field in user_data:
                        setattr(user, field, user_data[field])
                user.save(update_fields=["first_name", "last_name", "phone_number", "updated_at"])
                for field, value in validated_data.items():
                    setattr(instance, field, value)

                if photo_supplied:
                    if photo_upload is None:
                        instance.profile_photo = None
                    else:
                        optimized = optimize_profile_photo(photo_upload)
                        optimized.name = f"{uuid4().hex}.webp"
                        instance.profile_photo.save(optimized.name, optimized, save=False)

                instance.save()
                new_photo_name = instance.profile_photo.name if instance.profile_photo else ""
                if photo_supplied and old_photo_name and old_photo_name != new_photo_name:
                    _schedule_unshared_profile_photo_cleanup(storage, old_photo_name, using=using)
        except Exception:
            staged_photo_name = instance.profile_photo.name if instance.profile_photo else ""
            if photo_supplied and photo_upload is not None and staged_photo_name != old_photo_name:
                try:
                    _delete_unshared_profile_photo(storage, staged_photo_name, using=using)
                except Exception:
                    logger.warning("Could not remove a staged profile photo after rollback.", exc_info=True)
            instance.profile_photo = old_photo_name or None
            raise
        return instance


class ArtisanDiscoverySerializer(serializers.ModelSerializer):
    """Small public listing payload; professional links and biography stay off cards."""

    user_id = serializers.IntegerField(source="user.id", read_only=True)
    name = serializers.SerializerMethodField()
    profile_photo = serializers.ImageField(read_only=True)
    verification_status = serializers.CharField(source="verification.verification_status", read_only=True)
    services = serializers.SerializerMethodField()
    available_anywhere = serializers.BooleanField(read_only=True)
    has_rating = serializers.SerializerMethodField()
    review_count = serializers.IntegerField(read_only=True, default=0)
    is_promoted = serializers.BooleanField(read_only=True, default=False)
    promotion_campaign_id = serializers.UUIDField(source="_matched_promotion_campaign_id", read_only=True, allow_null=True, default=None)

    class Meta:
        model = ArtisanProfile
        fields = [
            "id", "user_id", "name", "business_name", "years_experience",
            "country", "state", "area", "profile_photo", "average_rating",
            "has_rating", "review_count", "available_anywhere",
            "verification_status", "services", "is_promoted", "promotion_campaign_id",
        ]

    def get_name(self, obj) -> str:
        return obj.user.get_full_name()

    def get_has_rating(self, obj) -> bool:
        return bool(obj.average_rating and obj.average_rating > 0)

    @extend_schema_field(ArtisanServiceSerializer(many=True))
    def get_services(self, obj):
        services = getattr(obj, "prefetched_artisan_services", None)
        if services is None:
            services = obj.artisan_services.select_related("service__category", "location").all()
        view = self.context.get("view")
        if getattr(view, "is_discovery_list", False):
            services = list(services)[:3]
        return ArtisanServiceSerializer(services, many=True, context=self.context).data


class ArtisanDiscoveryDetailSerializer(ArtisanDiscoverySerializer):
    """Full public profile payload; professional links are exposed only on detail."""

    bio = serializers.CharField(read_only=True)
    completion_rate = serializers.FloatField(read_only=True)
    cancellation_rate = serializers.FloatField(read_only=True)
    profile_status = serializers.CharField(read_only=True)
    social_media_1_platform = serializers.CharField(read_only=True)
    social_media_1_platform_label = serializers.CharField(source="get_social_media_1_platform_display", read_only=True)
    social_media_1_url = serializers.URLField(read_only=True, allow_blank=True)
    social_media_2_platform = serializers.CharField(read_only=True)
    social_media_2_platform_label = serializers.CharField(source="get_social_media_2_platform_display", read_only=True)
    social_media_2_url = serializers.URLField(read_only=True, allow_blank=True)
    portfolio_url = serializers.URLField(read_only=True, allow_blank=True)

    class Meta(ArtisanDiscoverySerializer.Meta):
        fields = ArtisanDiscoverySerializer.Meta.fields + [
            "bio", "completion_rate", "cancellation_rate", "profile_status",
            "social_media_1_platform", "social_media_1_platform_label", "social_media_1_url",
            "social_media_2_platform", "social_media_2_platform_label", "social_media_2_url", "portfolio_url",
        ]


class ArtisanProfessionalLinksReviewSerializer(serializers.ModelSerializer):
    """Restricted review payload. Deliberately excludes private account/contact fields."""

    name = serializers.SerializerMethodField()
    social_media_1_platform_label = serializers.CharField(source="get_social_media_1_platform_display", read_only=True)
    social_media_2_platform_label = serializers.CharField(source="get_social_media_2_platform_display", read_only=True)

    class Meta:
        model = ArtisanProfile
        fields = [
            "id", "name", "business_name", "profile_status",
            "social_media_1_platform", "social_media_1_platform_label", "social_media_1_url",
            "social_media_2_platform", "social_media_2_platform_label", "social_media_2_url", "portfolio_url",
        ]
        read_only_fields = fields

    def get_name(self, obj) -> str:
        return obj.user.get_full_name()
