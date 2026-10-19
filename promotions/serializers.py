import json

from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from home.models import ArtisanService, Institution, Locations
from .eligibility import promotion_eligibility
from .models import (
    PromotionApplication,
    PromotionApplicationDecision,
    PromotionApplicationEvidence,
)


class ServiceOfferIdListField(serializers.ListField):
    """Accept JSON arrays and JSON-encoded arrays used by multipart form posts."""

    def to_internal_value(self, data):
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], str):
            # A JSON-encoded array arrives as one value in multipart form posts.
            try:
                decoded = json.loads(data[0])
            except (TypeError, ValueError):
                decoded = data
            if isinstance(decoded, list):
                data = decoded
        elif isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                raise serializers.ValidationError("Submit service IDs as a list.") from None
        return super().to_internal_value(data)


class PromotionApplicationSubmissionSerializer(serializers.Serializer):
    promotion_type = serializers.ChoiceField(choices=PromotionApplication.PromotionType.choices)
    institution_id = serializers.PrimaryKeyRelatedField(
        source="institution",
        queryset=Institution.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    area_location_id = serializers.PrimaryKeyRelatedField(
        source="area_location",
        queryset=Locations.objects.all(),
        required=False,
        allow_null=True,
    )
    state_country = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    service_ids = ServiceOfferIdListField(
        child=serializers.IntegerField(min_value=1),
        min_length=1,
        max_length=40,
        write_only=True,
    )
    promotional_description = serializers.CharField(
        min_length=40,
        max_length=1200,
        trim_whitespace=True,
    )
    evidence_images = serializers.ListField(
        child=serializers.ImageField(),
        required=False,
        write_only=True,
        max_length=5,
    )

    def validate_evidence_images(self, images):
        for image in images:
            if image.size > 5 * 1024 * 1024:
                raise serializers.ValidationError("Each work sample must be 5 MB or smaller.")
        return images

    def validate(self, attrs):
        artisan = self.context["artisan"]
        eligibility = promotion_eligibility(artisan)
        if not eligibility["eligible"]:
            raise serializers.ValidationError({
                "eligibility": [item["message"] for item in eligibility["requirements"]],
            })

        promotion_type = attrs["promotion_type"]
        institution = attrs.get("institution")
        area_location = attrs.get("area_location")
        state_country = attrs.get("state_country", "").strip()
        state_name = attrs.get("state_name", "").strip()
        target_errors = {}

        if promotion_type == PromotionApplication.PromotionType.INSTITUTION:
            if institution is None:
                target_errors["institution_id"] = "Select an active institution from the supported catalogue."
            if area_location is not None or state_country or state_name:
                target_errors["target"] = "Institution Promotion accepts only an institution target."
        elif promotion_type == PromotionApplication.PromotionType.AREA:
            if area_location is None:
                target_errors["area_location_id"] = "Select a supported area or locality."
            if institution is not None or state_country or state_name:
                target_errors["target"] = "Area Promotion accepts only a supported area target."
        elif promotion_type == PromotionApplication.PromotionType.STATE:
            if not state_country:
                target_errors["state_country"] = "Select the country for the state target."
            if not state_name:
                target_errors["state_name"] = "Select a supported state or region."
            if institution is not None or area_location is not None:
                target_errors["target"] = "State Promotion accepts only a country and state target."
            if state_country and state_name and not Locations.objects.filter(
                country=state_country,
                state=state_name,
            ).exists():
                target_errors["state_name"] = "Select a state that exists in the supported location catalogue for that country."
        else:
            if institution is not None or area_location is not None or state_country or state_name:
                target_errors["target"] = "Homepage Feature does not use a geographical target."

        if target_errors:
            raise serializers.ValidationError(target_errors)

        service_ids = attrs.pop("service_ids")
        if len(service_ids) != len(set(service_ids)):
            raise serializers.ValidationError({"service_ids": "Choose each service offering only once."})
        offerings = list(
            ArtisanService.objects.filter(
                pk__in=service_ids,
                artisan=artisan,
                service__is_active=True,
            )
            .select_related("service__category", "location")
            .order_by("service__name", "pk")
        )
        if len(offerings) != len(service_ids):
            raise serializers.ValidationError({
                "service_ids": "Choose active service offerings from your own artisan profile.",
            })

        target_location = institution.location if promotion_type == PromotionApplication.PromotionType.INSTITUTION and institution else area_location
        for offering in offerings:
            # A NULL service location is the existing marketplace contract for a general,
            # anywhere offer. It is eligible for a targeted application without claiming
            # a physical office at the target.
            if offering.location_id is None or promotion_type == PromotionApplication.PromotionType.HOMEPAGE:
                continue
            if promotion_type in {PromotionApplication.PromotionType.INSTITUTION, PromotionApplication.PromotionType.AREA}:
                if target_location is None or offering.location_id != target_location.pk:
                    raise serializers.ValidationError({
                        "service_ids": f"{offering.service.name} must be offered at the exact selected target or available anywhere.",
                    })
            elif promotion_type == PromotionApplication.PromotionType.STATE and (
                offering.location.country != state_country or offering.location.state != state_name
            ):
                raise serializers.ValidationError({
                    "service_ids": f"{offering.service.name} must be offered in the exact selected country and state or available anywhere.",
                })

        attrs["state_country"] = state_country if promotion_type == PromotionApplication.PromotionType.STATE else ""
        attrs["state_name"] = state_name if promotion_type == PromotionApplication.PromotionType.STATE else ""
        attrs["service_offerings"] = offerings
        return attrs


class PromotionDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[
        (PromotionApplicationDecision.DecisionType.APPROVED, "Approve"),
        (PromotionApplicationDecision.DecisionType.CHANGES_REQUIRED, "Request Changes"),
        (PromotionApplicationDecision.DecisionType.REJECTED, "Reject"),
    ])
    reason = serializers.CharField(min_length=10, max_length=2000, trim_whitespace=True)


class PromotionApplicationEvidenceSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    revision_number = serializers.IntegerField(source="revision.revision_number", read_only=True)

    class Meta:
        model = PromotionApplicationEvidence
        fields = ["id", "image_url", "caption", "revision_number", "uploaded_at"]
        read_only_fields = fields

    def get_image_url(self, obj) -> str:
        request = self.context.get("request")
        path = reverse(
            "promotion-evidence",
            kwargs={
                "application_id": obj.revision.application_id,
                "evidence_id": obj.pk,
            },
        )
        return request.build_absolute_uri(path) if request else path


class PromotionApplicationLocationSnapshotSerializer(serializers.Serializer):
    id = serializers.CharField()
    country = serializers.CharField()
    state = serializers.CharField()
    area = serializers.CharField()


class PromotionApplicationServiceSnapshotSerializer(serializers.Serializer):
    artisan_service_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    service_name = serializers.CharField()
    category_name = serializers.CharField()
    location = PromotionApplicationLocationSnapshotSerializer(allow_null=True)
    starting_price = serializers.CharField(allow_null=True)


class PromotionApplicationLatestDecisionSerializer(serializers.Serializer):
    decision = serializers.CharField()
    decision_label = serializers.CharField()
    reason = serializers.CharField()
    created_at = serializers.DateTimeField()


class PromotionApplicationRevisionHistorySerializer(serializers.Serializer):
    revision_number = serializers.IntegerField()
    submitted_at = serializers.DateTimeField()
    snapshot = serializers.JSONField()
    evidence = PromotionApplicationEvidenceSerializer(many=True)


class PromotionApplicationApplicantSerializer(serializers.ModelSerializer):
    promotion_type_label = serializers.CharField(source="get_promotion_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    institution_id = serializers.UUIDField(read_only=True)
    area_location_id = serializers.UUIDField(read_only=True)
    service_ids = serializers.SerializerMethodField()
    services = serializers.SerializerMethodField()
    target = serializers.SerializerMethodField()
    evidence = serializers.SerializerMethodField()
    latest_decision = serializers.SerializerMethodField()
    latest_revision_number = serializers.SerializerMethodField()

    class Meta:
        model = PromotionApplication
        fields = [
            "id", "promotion_type", "promotion_type_label", "status", "status_label",
            "institution_id", "area_location_id", "state_country", "state_name", "target",
            "service_ids", "services", "promotional_description", "evidence",
            "latest_decision", "latest_revision_number", "submitted_at", "reviewed_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def _latest_revision(self, obj):
        revisions = getattr(obj, "prefetched_revisions", None)
        if revisions is not None:
            return revisions[-1] if revisions else None
        return obj.revisions.order_by("-revision_number").first()

    def _decisions(self, obj):
        decisions = getattr(obj, "prefetched_decisions", None)
        if decisions is not None:
            return decisions
        return list(obj.decisions.all())

    def get_service_ids(self, obj) -> list[int]:
        services = getattr(obj, "prefetched_services", None)
        if services is not None:
            return [int(service.pk) for service in services]
        return [int(value) for value in obj.services.values_list("pk", flat=True)]

    @extend_schema_field(PromotionApplicationServiceSnapshotSerializer(many=True))
    def get_services(self, obj):
        revision = self._latest_revision(obj)
        if revision and isinstance(revision.snapshot, dict):
            return revision.snapshot.get("services", [])
        return []

    @extend_schema_field(serializers.JSONField())
    def get_target(self, obj):
        revision = self._latest_revision(obj)
        if revision and isinstance(revision.snapshot, dict):
            return revision.snapshot.get("target", {})
        return {}

    @extend_schema_field(PromotionApplicationEvidenceSerializer(many=True))
    def get_evidence(self, obj):
        revision = self._latest_revision(obj)
        if revision is None:
            return []
        evidence = getattr(revision, "prefetched_evidence", None)
        if evidence is None:
            evidence = revision.evidence.all()
        return PromotionApplicationEvidenceSerializer(
            evidence,
            many=True,
            context=self.context,
        ).data

    @extend_schema_field(PromotionApplicationLatestDecisionSerializer(allow_null=True))
    def get_latest_decision(self, obj):
        decisions = self._decisions(obj)
        decision = decisions[0] if decisions else None
        if decision is None:
            return None
        return {
            "decision": decision.decision_type,
            "decision_label": decision.get_decision_type_display(),
            "reason": decision.reason,
            "created_at": decision.created_at,
        }

    def get_latest_revision_number(self, obj) -> int | None:
        revision = self._latest_revision(obj)
        return revision.revision_number if revision else None


class PromotionApplicationQueueSerializer(serializers.ModelSerializer):
    promotion_type_label = serializers.CharField(source="get_promotion_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    artisan_id = serializers.UUIDField(source="artisan.id", read_only=True)
    artisan_name = serializers.SerializerMethodField()
    business_name = serializers.CharField(source="artisan.business_name", read_only=True)
    target = serializers.SerializerMethodField()
    latest_revision_number = serializers.SerializerMethodField()

    class Meta:
        model = PromotionApplication
        fields = [
            "id", "promotion_type", "promotion_type_label", "status", "status_label",
            "artisan_id", "artisan_name", "business_name", "target", "submitted_at",
            "reviewed_at", "latest_revision_number",
        ]
        read_only_fields = fields

    def _latest_revision(self, obj):
        revisions = getattr(obj, "prefetched_revisions", None)
        return revisions[-1] if revisions else None

    def get_latest_revision_number(self, obj) -> int | None:
        revision = self._latest_revision(obj)
        return revision.revision_number if revision else None

    def get_artisan_name(self, obj) -> str:
        return obj.artisan.user.get_full_name()

    @extend_schema_field(serializers.JSONField())
    def get_target(self, obj):
        revision = self._latest_revision(obj)
        return revision.snapshot.get("target", {}) if revision and isinstance(revision.snapshot, dict) else {}


class PromotionApplicationDecisionSerializer(serializers.ModelSerializer):
    decision_label = serializers.CharField(source="get_decision_type_display", read_only=True)
    actor_name = serializers.CharField(source="actor_name_snapshot", read_only=True)
    revision_number = serializers.IntegerField(source="revision.revision_number", read_only=True)

    class Meta:
        model = PromotionApplicationDecision
        fields = ["id", "decision_type", "decision_label", "reason", "actor_name", "revision_number", "created_at"]
        read_only_fields = fields


class PromotionApplicationReviewDetailSerializer(PromotionApplicationQueueSerializer):
    institution_id = serializers.UUIDField(read_only=True)
    area_location_id = serializers.UUIDField(read_only=True)
    current_revision_snapshot = serializers.SerializerMethodField()
    revision_history = serializers.SerializerMethodField()
    decisions = serializers.SerializerMethodField()

    class Meta(PromotionApplicationQueueSerializer.Meta):
        fields = PromotionApplicationQueueSerializer.Meta.fields + [
            "promotional_description", "state_country", "state_name", "institution_id",
            "area_location_id", "current_revision_snapshot", "revision_history",
            "decisions",
        ]
        read_only_fields = fields

    def _revision_list(self, obj):
        revisions = getattr(obj, "prefetched_revisions", None)
        if revisions is not None:
            return revisions
        return list(obj.revisions.all())

    @extend_schema_field(serializers.JSONField())
    def get_current_revision_snapshot(self, obj):
        revisions = self._revision_list(obj)
        if not revisions:
            return {}
        return revisions[-1].snapshot

    @extend_schema_field(PromotionApplicationRevisionHistorySerializer(many=True))
    def get_revision_history(self, obj):
        history = []
        for revision in self._revision_list(obj):
            evidence = getattr(revision, "prefetched_evidence", None)
            if evidence is None:
                evidence = revision.evidence.all()
            history.append({
                "revision_number": revision.revision_number,
                "submitted_at": revision.submitted_at,
                "snapshot": revision.snapshot,
                "evidence": PromotionApplicationEvidenceSerializer(
                    evidence,
                    many=True,
                    context=self.context,
                ).data,
            })
        return history

    @extend_schema_field(PromotionApplicationDecisionSerializer(many=True))
    def get_decisions(self, obj):
        decisions = getattr(obj, "prefetched_decisions", None)
        if decisions is None:
            decisions = obj.decisions.all()
        return PromotionApplicationDecisionSerializer(decisions, many=True).data
