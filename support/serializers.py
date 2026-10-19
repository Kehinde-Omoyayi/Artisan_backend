from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from rest_framework import serializers

from booking.models import Booking
from conversations.models import Conversation
from promotions.models import PromotionCampaign
from .validation import SUPPORT_DESCRIPTION_MINIMUM_MESSAGE, normalize_support_description
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field

from .models import (
    SupportCase,
    SupportCaseEvent,
    SupportFinancialAction,
)
from .permissions import user_has_support_role


class SupportCaseSerializer(serializers.ModelSerializer):
    opened_by_name = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    conversation_id = serializers.SerializerMethodField()

    class Meta:
        model = SupportCase
        fields = [
            "id", "reference", "booking", "conversation_id", "promotion_campaign", "opened_by",
            "opened_by_name", "assigned_to", "assigned_to_name",
            "case_type", "status", "priority", "subject",
            "description", "resolution_code", "resolution_note",
            "resolved_at", "closed_at", "created_at", "updated_at",
            "can_manage", "can_message",
        ]
        read_only_fields = [
            "id", "reference", "opened_by", "assigned_to", "promotion_campaign",
            "status", "resolution_code", "resolution_note",
            "resolved_at", "closed_at", "created_at", "updated_at",
            "can_manage", "can_message",
        ]

    can_manage = serializers.SerializerMethodField()
    can_message = serializers.SerializerMethodField()

    def _viewer(self):
        request = self.context.get("request")
        return getattr(request, "user", None) if request else None

    @staticmethod
    def _is_conversation_participant(conversation, user):
        if not conversation or not user or not getattr(user, "is_authenticated", False):
            return False
        return bool(
            conversation.customer_id == user.id
            or (conversation.artisan_id and conversation.artisan.user_id == user.id)
            or conversation.participants.filter(pk=user.id).exists()
        )

    @staticmethod
    def _related_conversation(obj):
        if obj.conversation_id:
            conversation = obj.conversation
        elif obj.booking_id:
            try:
                conversation = obj.booking.request.conversation
            except ObjectDoesNotExist:
                return None
        else:
            return None

        if obj.booking_id and not (
            conversation.conversation_type == Conversation.ConversationType.SERVICE
            and conversation.service_request_id == obj.booking.request_id
            and conversation.customer_id == obj.booking.customer.user_id
            and conversation.artisan_id == obj.booking.artisan_id
            and obj.booking.request.requester_id == obj.booking.customer.user_id
        ):
            return None
        return conversation

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_can_manage(self, obj):
        return user_has_support_role(self._viewer())

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_can_message(self, obj):
        user = self._viewer()
        if not user or obj.status in {SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED}:
            return False
        conversation = self._related_conversation(obj)
        is_participant = (
            obj.opened_by_id == user.id
            or bool(obj.booking_id and (
                obj.booking.customer.user_id == user.id
                or obj.booking.artisan.user_id == user.id
            ))
            or bool(obj.promotion_campaign_id and obj.promotion_campaign.artisan.user_id == user.id)
            or self._is_conversation_participant(conversation, user)
        )
        return is_participant or user_has_support_role(user)

    @extend_schema_field(OpenApiTypes.STR)
    def get_opened_by_name(self, obj):
        return (
            obj.opened_by.get_full_name() or obj.opened_by.email
            if obj.opened_by else None
        )

    @extend_schema_field(OpenApiTypes.STR)
    def get_assigned_to_name(self, obj):
        return (
            obj.assigned_to.get_full_name() or obj.assigned_to.email
            if obj.assigned_to else None
        )

    @extend_schema_field(OpenApiTypes.UUID)
    def get_conversation_id(self, obj):
        conversation = self._related_conversation(obj)
        if not self._is_conversation_participant(conversation, self._viewer()):
            return None
        return str(conversation.pk)


class SupportCaseCreateSerializer(serializers.ModelSerializer):
    conversation_id = serializers.PrimaryKeyRelatedField(
        source="conversation",
        queryset=Conversation.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
    )
    promotion_campaign = serializers.PrimaryKeyRelatedField(
        queryset=PromotionCampaign.objects.all(),
        required=False,
        allow_null=True,
    )
    description = serializers.CharField(max_length=10000, trim_whitespace=True)

    class Meta:
        model = SupportCase
        fields = [
            "conversation_id", "promotion_campaign", "case_type", "priority",
            "subject", "description",
        ]

    def to_internal_value(self, data):
        if isinstance(data, dict) and "booking" in data:
            raise serializers.ValidationError({
                "booking": "Booking links are resolved from your authenticated conversation. Open the relevant conversation or request summary instead of entering a booking ID."
            })
        return super().to_internal_value(data)

    def validate_description(self, value):
        try:
            return normalize_support_description(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        request = self.context["request"]
        conversation = attrs.get("conversation")
        promotion_campaign = attrs.get("promotion_campaign")
        case_type = attrs.get("case_type")
        if conversation and promotion_campaign:
            raise serializers.ValidationError({
                "conversation_id": "A support case can reference a conversation or a promotion campaign, not both."
            })

        booking = None
        if conversation:
            # Re-fetch with its actual relationship fields. Never trust the submitted UUID
            # until the authenticated user is confirmed as a conversation participant.
            conversation = (
                Conversation.objects
                .select_related("customer", "artisan__user", "service_request")
                .prefetch_related("participants")
                .filter(pk=conversation.pk)
                .first()
            )
            is_participant = bool(conversation and (
                conversation.customer_id == request.user.id
                or (conversation.artisan_id and conversation.artisan.user_id == request.user.id)
                or conversation.participants.filter(pk=request.user.id).exists()
            ))
            if not is_participant:
                raise serializers.ValidationError({
                    "conversation_id": "The conversation could not be found or you are not a participant."
                })

            if conversation.conversation_type == Conversation.ConversationType.SERVICE and conversation.service_request_id:
                if (
                    not conversation.customer_id
                    or not conversation.artisan_id
                    or conversation.service_request.requester_id != conversation.customer_id
                ):
                    raise serializers.ValidationError({
                        "conversation_id": "This service conversation is not linked to a valid customer request."
                    })
                booking = (
                    Booking.objects
                    .filter(
                        request_id=conversation.service_request_id,
                        customer__user_id=conversation.customer_id,
                        artisan_id=conversation.artisan_id,
                        request__requester_id=conversation.customer_id,
                    )
                    .order_by("-created_at")
                    .first()
                )
            attrs["conversation"] = conversation
            attrs["booking"] = booking

        if promotion_campaign:
            is_owner = promotion_campaign.artisan.user_id == request.user.id
            is_live = (
                promotion_campaign.status == PromotionCampaign.Status.ACTIVE
                and promotion_campaign.started_at is not None
                and promotion_campaign.started_at <= timezone.now()
                and promotion_campaign.expires_at is not None
                and promotion_campaign.expires_at > timezone.now()
            )
            if not is_owner and not is_live:
                raise serializers.ValidationError({"promotion_campaign": "Only the campaign owner or a visitor reporting a currently public campaign can open a case."})
            if case_type not in {SupportCase.CaseType.REPORT, SupportCase.CaseType.OTHER, SupportCase.CaseType.PAYMENT}:
                raise serializers.ValidationError({"case_type": "Use Report, Other or Payment for a promotion-related support case."})

        if case_type == SupportCase.CaseType.DISPUTE:
            if not conversation:
                raise serializers.ValidationError({
                    "conversation_id": "A dispute must be opened from the relevant service conversation so its verified booking and escrow state can be attached automatically."
                })
            if not booking:
                raise serializers.ValidationError({
                    "conversation_id": "No booking is linked to this conversation yet. Complete the legitimate negotiation and booking flow before opening a dispute."
                })
            raise serializers.ValidationError({
                "case_type": "Use the dispute action in this service conversation so the booking and escrow state are updated together."
            })

        if case_type == SupportCase.CaseType.PAYMENT and not (booking or promotion_campaign):
            raise serializers.ValidationError({
                "conversation_id": "A payment case must be linked to a booking or promotion. Open it from the relevant conversation or promotion summary."
            })

        return attrs


class SupportCaseResolveSerializer(serializers.Serializer):
    resolution_code = serializers.ChoiceField(
        choices=[
            (value, label)
            for value, label in SupportCase.ResolutionCode.choices
            if value not in {
                SupportCase.ResolutionCode.PAYOUT,
                SupportCase.ResolutionCode.PARTIAL_PAYOUT,
            }
        ]
    )
    resolution_note = serializers.CharField(min_length=5)


class SupportCaseEventSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportCaseEvent
        fields = [
            "id", "action", "note", "performed_by",
            "performed_by_name", "metadata", "created_at",
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_performed_by_name(self, obj):
        return (
            obj.performed_by.get_full_name() or obj.performed_by.email
            if obj.performed_by else None
        )


class SupportCaseAssignSerializer(serializers.Serializer):
    staff_user_id = serializers.IntegerField()


class SupportCaseNoteSerializer(serializers.Serializer):
    note = serializers.CharField(min_length=2, max_length=5000)


class SupportCaseMessageSerializer(serializers.Serializer):
    message = serializers.CharField(min_length=2, max_length=5000, trim_whitespace=True)


class SupportCapabilitiesSerializer(serializers.Serializer):
    is_support_staff = serializers.BooleanField()
    is_superuser = serializers.BooleanField()
    roles = serializers.ListField(child=serializers.CharField())
    can_claim_cases = serializers.BooleanField()
    can_assign_cases = serializers.BooleanField()
    can_add_private_notes = serializers.BooleanField()
    can_resolve_cases = serializers.BooleanField()
    can_close_cases = serializers.BooleanField()
    can_view_refunds = serializers.BooleanField()
    can_request_refunds = serializers.BooleanField()
    can_approve_refunds = serializers.BooleanField()
    can_execute_refunds = serializers.BooleanField()
    can_reconcile_refunds = serializers.BooleanField()
    can_manage_staff = serializers.BooleanField()
    can_manage_roles = serializers.BooleanField()
    can_view_finance = serializers.BooleanField()
    can_review_promotions = serializers.BooleanField()


class SupportCaseRefundEligiblePaymentSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    reference = serializers.CharField()
    status = serializers.CharField()
    currency = serializers.CharField()
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    remaining_amount = serializers.DecimalField(max_digits=12, decimal_places=2)


class SupportRefundRequestSerializer(serializers.Serializer):
    payment_id = serializers.UUIDField()
    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    reason = serializers.CharField(min_length=5, max_length=2000)
    idempotency_key = serializers.CharField(
        required=True,
        max_length=255,
        allow_blank=False,
    )


class SupportRefundApprovalSerializer(serializers.Serializer):
    approval_note = serializers.CharField(min_length=5, max_length=2000)


class SupportRefundRejectionSerializer(serializers.Serializer):
    rejection_note = serializers.CharField(min_length=5, max_length=2000)


class SupportFinancialActionSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    executed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportFinancialAction
        fields = [
            "id", "reference", "case", "payment", "promotion_payment", "action_type",
            "amount", "reason", "status", "requested_by",
            "requested_by_name", "approved_by", "approved_by_name",
            "executed_by", "executed_by_name", "provider_status",
            "failure_reason", "requested_at", "approved_at",
            "submitted_at", "completed_at", "updated_at",
        ]
        read_only_fields = fields

    def get_amount(self, obj) -> float:
        return obj.amount_kobo / 100

    def _name(self, value) -> str | None:
        return (
            value.get_full_name() or value.email
            if value else None
        )

    def get_requested_by_name(self, obj) -> str | None:
        return self._name(obj.requested_by)

    def get_approved_by_name(self, obj) -> str | None:
        return self._name(obj.approved_by)

    def get_executed_by_name(self, obj) -> str | None:
        return self._name(obj.executed_by)


class SupportFinancialActionFinanceSerializer(
    SupportFinancialActionSerializer
):
    class Meta(SupportFinancialActionSerializer.Meta):
        fields = (
            SupportFinancialActionSerializer.Meta.fields
            + ["provider_refund_id", "provider_ref", "provider_payload"]
        )
        read_only_fields = fields


class SupportFinancialActionPublicSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()

    class Meta:
        model = SupportFinancialAction
        fields = [
            "id", "reference", "case", "payment", "promotion_payment", "action_type",
            "amount", "status", "requested_at", "approved_at",
            "submitted_at", "completed_at",
        ]
        read_only_fields = fields

    def get_amount(self, obj) -> float:
        return obj.amount_kobo / 100


class SupportRoleUpdateSerializer(serializers.Serializer):
    roles = serializers.ListField(
        child=serializers.ChoiceField(
            choices=[
                "CUSTOMER_SUPPORT_AGENT",
                "CUSTOMER_SUPPORT_SUPERVISOR",
                "CUSTOMER_SUPPORT_FINANCE",
                "CUSTOMER_SUPPORT_ADMIN",
            ]
        ),
        allow_empty=True,
    )


class SupportStaffSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    name = serializers.CharField()
    roles = serializers.ListField(child=serializers.CharField())
    security_questions_registered = serializers.BooleanField(required=False)
