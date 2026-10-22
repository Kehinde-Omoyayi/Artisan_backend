from django.core.exceptions import ValidationError as DjangoValidationError
from datetime import date
from decimal import Decimal

from rest_framework import serializers

from home.models import (
    ArtisanProfile,
    ArtisanService,
    CustomerProfile,
    Institution,
    Locations,
    Service,
    ServiceCategory,
)

from .image_utils import optimize_profile_photo
from .models import ServiceRequest


class NINVerificationSerializer(serializers.Serializer):
    nin = serializers.RegexField(
        regex=r"^\d{11}$", write_only=True, help_text="Exactly 11 numeric digits."
    )
    consent = serializers.BooleanField(
        help_text="Must be true to authorize the NIN lookup."
    )

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError(
                "Explicit consent is required before a NIN lookup."
            )
        return value


class BVNVerificationSerializer(serializers.Serializer):
    bvn = serializers.RegexField(
        regex=r"^\d{11}$", write_only=True, help_text="Exactly 11 numeric digits."
    )
    consent = serializers.BooleanField(
        help_text="Must be true to authorize the BVN lookup."
    )

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError(
                "Explicit consent is required before a BVN lookup."
            )
        return value


class OTPSerializer(serializers.Serializer):
    otp = serializers.RegexField(
        regex=r"^\d{6}$", write_only=True, help_text="Exactly 6 numeric digits."
    )


class VerificationStatusSerializer(serializers.Serializer):
    status = serializers.CharField()
    method = serializers.CharField(allow_blank=True)
    tier = serializers.IntegerField()
    provider = serializers.CharField(allow_blank=True)
    reference = serializers.CharField(allow_blank=True)
    verified_at = serializers.DateTimeField(allow_null=True)
    identity_name_available = serializers.BooleanField()


class VerificationSuccessSerializer(serializers.Serializer):
    status = serializers.CharField()
    verification_tier = serializers.IntegerField()
    reference = serializers.CharField(allow_blank=True)
    next_step = serializers.CharField()


class BVNInitiateResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField()
    methods = serializers.ListField(child=serializers.DictField())
    next_step = serializers.CharField()
    next_endpoint = serializers.CharField()


class VerificationErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(required=False)
    status = serializers.CharField(required=False)
    message = serializers.CharField(required=False)


class BVNMethodResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField()
    next_step = serializers.CharField()
    next_endpoint = serializers.CharField()


def resolve_existing_location(value):
    """Resolve country, state and area to an existing Locations record."""
    if value is None or isinstance(value, Locations):
        return value

    matches = list(
        Locations.objects.filter(
            country__iexact=str(value.get("country", "")).strip(),
            state__iexact=str(value.get("state", "")).strip(),
            area__iexact=str(value.get("area", "")).strip(),
        ).order_by("pk")[:2]
    )

    if not matches:
        raise serializers.ValidationError(
            "No matching location exists. Select a location from the catalogue."
        )

    if len(matches) > 1:
        raise serializers.ValidationError(
            "Duplicate location records exist. Please resolve them first."
        )

    return matches[0]


class LocationSerializer(serializers.ModelSerializer):
    """The single location payload used everywhere the API reads or writes a location.

    Read::

        {"id": "...", "country": "Nigeria", "state": "Lagos", "area": "Ikeja",
         "closest_institution": {"id": "...", "name": "...", "campus": "...", "abbreviation": "..."}}

    Every location is a catalogue row, so artisan service coverage, service requests,
    bookings and discovery describe a place the same way, and the closest institution
    travels with the location instead of being re-derived by each caller.
    """

    closest_institution = serializers.SerializerMethodField()

    class Meta:
        model = Locations
        fields = ["id", "country", "state", "area", "closest_institution"]
        read_only_fields = ["id"]

    def validate_country(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Country is required.")
        return value

    def validate_state(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("State is required.")
        return value

    def validate_area(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Area is required.")
        return value

    def get_closest_institution(self, location):
        """The institution registered at this location, or None."""
        if location is None:
            return None
        cache = getattr(self, "_closest_institution_cache", None)
        if cache is None:
            cache = self._closest_institution_cache = {}
        key = str(location.pk)
        if key not in cache:
            candidates = Institution.objects.filter(location=location, is_active=True)
            request = self.context.get("request")
            selected = (
                getattr(request, "query_params", {}).get("institution_id")
                if request
                else None
            )
            if selected:
                try:
                    from uuid import UUID

                    candidates = candidates.filter(pk=UUID(str(selected)))
                except ValueError:
                    candidates = candidates.none()
            matches = list(
                candidates.order_by("name", "campus").values(
                    "id", "name", "campus", "abbreviation"
                )[:2]
            )
            # A city can host many institutions. Alphabetical order does not prove
            # which one is nearest; return no inferred choice when ambiguous.
            row = matches[0] if len(matches) == 1 else None
            if row:
                row["id"] = str(row["id"])
            cache[key] = row
        return cache[key]


class LocationField(serializers.Field):
    """Nested location input/output that always resolves to one catalogue location.

    Accepts ``{"id": "<uuid>"}`` or the legacy ``{"country", "state", "area"}`` pair,
    and always renders through :class:`LocationSerializer`, so exactly one location
    shape exists in every response.
    """

    default_error_messages = {
        "invalid": "Provide a location id, or country, state and area.",
        "missing": "No matching location exists. Select a location from the catalogue.",
        "incomplete": "Country, state and area are all required.",
    }

    def to_internal_value(self, data):
        if isinstance(data, Locations):
            return data
        if not isinstance(data, dict):
            self.fail("invalid")

        institution_id = str(data.get("institution_id") or "").strip()
        if institution_id:
            try:
                institution = Institution.objects.select_related("location").get(
                    pk=institution_id, is_active=True
                )
            except (
                Institution.DoesNotExist,
                ValueError,
                TypeError,
                DjangoValidationError,
            ):
                raise serializers.ValidationError(
                    "Choose an active institution from the catalogue."
                )
            location = institution.location
            if data.get("id") and str(data["id"]).replace("-", "") != str(
                location.pk
            ).replace("-", ""):
                raise serializers.ValidationError(
                    "The institution does not belong to the selected location."
                )
            for key in ("country", "state", "area"):
                if (
                    data.get(key)
                    and str(data[key]).strip().casefold()
                    != getattr(location, key).casefold()
                ):
                    raise serializers.ValidationError(
                        "The institution does not belong to the selected location."
                    )
            return location
        identifier = str(data.get("id") or "").strip()
        if identifier:
            try:
                return Locations.objects.get(pk=identifier)
            except (
                Locations.DoesNotExist,
                ValueError,
                TypeError,
                DjangoValidationError,
            ):
                self.fail("missing")

        if not all(
            str(data.get(field) or "").strip() for field in ("country", "state", "area")
        ):
            self.fail("incomplete")
        if getattr(getattr(self.parent, "Meta", None), "model", None) is ServiceRequest:
            try:
                return resolve_existing_location(data)
            except serializers.ValidationError:
                from .geography import canonical_location

                location = canonical_location(
                    data.get("country"), data.get("state"), data.get("area")
                )
                if location is None:
                    raise serializers.ValidationError(
                        "Choose a location from the geographic catalogue."
                    )
                # Resolve to the same canonical Locations model when the request is saved.
                return location
        return resolve_existing_location(data)

    def to_representation(self, value):
        if value is None:
            return None
        return LocationSerializer(value, context=self.context).data


class UppercaseChoiceField(serializers.ChoiceField):
    def to_internal_value(self, data):
        if isinstance(data, str):
            data = data.strip().upper()
        return super().to_internal_value(data)


class ArtisanMatchedServiceRequestSerializer(serializers.ModelSerializer):
    """Public request details for an artisan whose active services match the request."""

    age_seconds = serializers.SerializerMethodField()

    def get_age_seconds(self, obj):
        from django.utils import timezone

        return max(0, int((timezone.now() - obj.created_at).total_seconds()))

    category_name = serializers.CharField(source="category.name", read_only=True)
    service_name = serializers.CharField(
        source="service.name", read_only=True, allow_null=True
    )
    location = LocationField(read_only=True)

    class Meta:
        model = ServiceRequest
        fields = [
            "id",
            "category_name",
            "service_name",
            "location",
            "preferred_date",
            "preferred_time_start",
            "preferred_time_end",
            "preferred_time_note",
            "urgency",
            "budget",
            "description",
            "additional_details",
            "age_seconds",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ServiceRequestSerializer(serializers.ModelSerializer):
    requester_id = serializers.IntegerField(
        source="requester.id",
        read_only=True,
    )

    category_name = serializers.SlugRelatedField(
        source="category",
        slug_field="name",
        queryset=ServiceCategory.objects.filter(
            services__is_active=True,
        ).distinct(),
        required=False,
    )

    service_name = serializers.SlugRelatedField(
        source="service",
        slug_field="name",
        queryset=Service.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    location = LocationField(required=False)

    urgency = UppercaseChoiceField(
        choices=ServiceRequest._meta.get_field("urgency").choices,
    )

    class Meta:
        model = ServiceRequest
        fields = [
            "id",
            "requester_id",
            "category_name",
            "service_name",
            "location",
            "preferred_date",
            "preferred_time_start",
            "preferred_time_end",
            "preferred_time_note",
            "urgency",
            "budget",
            "description",
            "additional_details",
            "status",
            "attended_at",
            "archived_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "requester_id",
            "status",
            "attended_at",
            "archived_at",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        service = attrs.get(
            "service",
            getattr(self.instance, "service", None),
        )
        category = attrs.get(
            "category",
            getattr(self.instance, "category", None),
        )

        if self.instance is None and service is None:
            raise serializers.ValidationError(
                {"service_name": "Select a service from the service catalogue."}
            )

        if "service" in attrs and service is None:
            raise serializers.ValidationError(
                {"service_name": "A service must be selected."}
            )

        if self.instance is None and attrs.get("preferred_date") is None:
            raise serializers.ValidationError(
                {"preferred_date": "Choose a preferred date before sending a request."}
            )
        if self.instance is None and attrs.get("location") is None:
            raise serializers.ValidationError({"location": "Select a location."})

        preferred_start = attrs.get(
            "preferred_time_start",
            getattr(self.instance, "preferred_time_start", None),
        )
        preferred_end = attrs.get(
            "preferred_time_end",
            getattr(self.instance, "preferred_time_end", None),
        )
        if preferred_start and preferred_end and preferred_end <= preferred_start:
            raise serializers.ValidationError(
                {"preferred_time_end": "End time must be later than start time."}
            )

        if service is not None:
            # Selecting a service automatically determines its category
            # when the category name is omitted or the service is changed.
            if "service" in attrs and "category" not in attrs:
                attrs["category"] = service.category
                category = service.category
            elif category is None:
                attrs["category"] = service.category
                category = service.category

            if category.pk != service.category_id:
                raise serializers.ValidationError(
                    {
                        "service_name": (
                            "The selected service does not belong "
                            "to the selected category."
                        )
                    }
                )

        return attrs

    def _resolve_geographic_location(self, validated_data):
        location = validated_data.get("location")
        if isinstance(location, dict):
            # Case-insensitive reuse avoids a second representation for an existing place.
            existing = Locations.objects.filter(
                **{f"{key}__iexact": value for key, value in location.items()}
            ).first()
            validated_data["location"] = (
                existing or Locations.objects.get_or_create(**location)[0]
            )

    def create(self, validated_data):
        self._resolve_geographic_location(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        self._resolve_geographic_location(validated_data)
        return super().update(instance, validated_data)

    def validate_preferred_date(self, value):
        if value is None:
            raise serializers.ValidationError(
                "Choose a preferred date before sending a request."
            )
        if value < date.today():
            raise serializers.ValidationError("Preferred date cannot be in the past.")
        return value

    def validate_preferred_time_note(self, value):
        return value.strip()

    def validate_additional_details(self, value):
        value = value.strip()
        if len(value) > 10000:
            raise serializers.ValidationError(
                "Additional details must be 10,000 characters or fewer."
            )
        return value

    def validate_budget(self, value):
        if value is not None and value < Decimal("0.00"):
            raise serializers.ValidationError("Budget cannot be negative.")
        return value

    def validate_description(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Description cannot be empty.")
        if len(value) > 10000:
            raise serializers.ValidationError(
                "Description must be 10,000 characters or fewer."
            )
        return value


class InstitutionSerializer(serializers.ModelSerializer):
    location_id = serializers.UUIDField(source="location.id", read_only=True)
    country = serializers.CharField(source="location.country", read_only=True)
    state = serializers.CharField(source="location.state", read_only=True)
    area = serializers.CharField(source="location.area", read_only=True)

    class Meta:
        model = Institution
        fields = [
            "id",
            "name",
            "campus",
            "abbreviation",
            "location_id",
            "country",
            "state",
            "area",
            "abbreviations",
            "aliases",
            "source_url",
        ]
        read_only_fields = fields


class ServiceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceCategory
        fields = ["name"]


class ServiceSerializer(serializers.ModelSerializer):
    category_name = serializers.SlugRelatedField(
        source="category",
        slug_field="name",
        queryset=ServiceCategory.objects.all(),
    )

    class Meta:
        model = Service
        fields = [
            "id",
            "category_name",
            "name",
            "description",
            "is_active",
        ]
        read_only_fields = ["id"]


class ArtisanServiceSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(read_only=True)
    service_id = serializers.IntegerField(read_only=True)

    service_name = serializers.SlugRelatedField(
        source="service",
        slug_field="name",
        queryset=Service.objects.filter(is_active=True),
    )

    category_name = serializers.CharField(
        source="service.category.name",
        read_only=True,
    )

    location = LocationField(
        required=False,
        allow_null=True,
    )

    class Meta:
        model = ArtisanService
        fields = [
            "id",
            "service_id",
            "category_name",
            "service_name",
            "location",
            "starting_price",
        ]

    def validate(self, attrs):
        service = attrs.get(
            "service",
            getattr(self.instance, "service", None),
        )

        request = self.context.get("request")
        artisan = None

        if request and request.user.is_authenticated:
            try:
                artisan = request.user.artisan_profile
            except (AttributeError, ArtisanProfile.DoesNotExist):
                artisan = None

        # Enforce one selected category per artisan when the profile has
        # a service_category foreign key configured.
        selected_category_id = getattr(
            artisan,
            "service_category_id",
            None,
        )

        if (
            service is not None
            and selected_category_id is not None
            and service.category_id != selected_category_id
        ):
            raise serializers.ValidationError(
                {
                    "service_name": (
                        "This service is outside your selected service category."
                    )
                }
            )

        return attrs

    def validate_starting_price(self, value):
        if value is not None and value < Decimal("0.00"):
            raise serializers.ValidationError("Starting price cannot be negative.")
        return value


class ArtisanDiscoverySerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(
        source="user.id",
        read_only=True,
    )
    name = serializers.SerializerMethodField()
    profile_photo = serializers.ImageField(read_only=True)

    verification_status = serializers.CharField(
        source="verification.verification_status",
        read_only=True,
    )

    services = serializers.SerializerMethodField()

    class Meta:
        model = ArtisanProfile
        fields = [
            "id",
            "user_id",
            "name",
            "business_name",
            "bio",
            "years_experience",
            "country",
            "state",
            "area",
            "profile_photo",
            "average_rating",
            "completion_rate",
            "cancellation_rate",
            "profile_status",
            "verification_status",
            "services",
        ]

    def get_name(self, obj):
        return obj.user.get_full_name()

    def get_services(self, obj):
        services = getattr(
            obj,
            "prefetched_artisan_services",
            None,
        )

        if services is None:
            services = obj.artisan_services.select_related("service", "location").all()

        return ArtisanServiceSerializer(
            services,
            many=True,
            context=self.context,
        ).data
