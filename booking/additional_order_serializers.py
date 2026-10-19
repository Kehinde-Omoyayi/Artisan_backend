from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import BookingAdditionalOffer, BookingAdditionalOrder


class AdditionalOrderOfferSerializer(serializers.ModelSerializer):
    created_by_id = serializers.IntegerField(read_only=True)
    accepted_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    parent_offer_id = serializers.UUIDField(read_only=True, allow_null=True)
    currency = serializers.CharField(default="NGN", read_only=True)

    class Meta:
        model = BookingAdditionalOffer
        fields = [
            "id", "revision", "created_by_id", "accepted_by_id", "parent_offer_id", "amount",
            "description", "status", "currency", "responded_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class AdditionalOrderCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    description = serializers.CharField(max_length=3000, trim_whitespace=True)


class AdditionalOrderResponseSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("accept", "decline", "counter"))
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False)
    description = serializers.CharField(max_length=3000, trim_whitespace=True, required=False, allow_blank=False)

    def validate(self, attrs):
        if attrs["action"] == "counter" and not attrs.get("amount"):
            raise serializers.ValidationError({"amount": "An amount is required for a counter-offer."})
        if attrs["action"] == "counter" and not attrs.get("description", "").strip():
            raise serializers.ValidationError({"description": "A scope description is required for a counter-offer."})
        if attrs["action"] != "counter" and ("amount" in attrs or "description" in attrs):
            raise serializers.ValidationError("Only counter-offers accept an amount or description.")
        return attrs


class AdditionalOrderPaymentSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    service_amount = serializers.SerializerMethodField()
    customer_fee = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    authorization_url = serializers.SerializerMethodField()
    verified_at = serializers.DateTimeField(allow_null=True)
    held_at = serializers.DateTimeField(allow_null=True)
    released_at = serializers.DateTimeField(allow_null=True)

    def get_service_amount(self, obj) -> Decimal:
        return Decimal(obj.service_amount_kobo) / Decimal("100")

    def get_customer_fee(self, obj) -> Decimal:
        return Decimal(obj.customer_fee_kobo) / Decimal("100")

    def get_total_amount(self, obj) -> Decimal:
        return Decimal(obj.total_amount_kobo) / Decimal("100")

    def get_authorization_url(self, obj) -> str | None:
        request = self.context.get("request")
        if not request or obj.customer_id != request.user.id:
            return None
        payload = obj.provider_payload if isinstance(obj.provider_payload, dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        link = data.get("link")
        return str(link) if link else None


class AdditionalOrderSerializer(serializers.ModelSerializer):
    created_by_id = serializers.IntegerField(read_only=True)
    accepted_offer_id = serializers.UUIDField(read_only=True, allow_null=True)
    offers = AdditionalOrderOfferSerializer(many=True, read_only=True)
    current_offer = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()
    can_pay = serializers.SerializerMethodField()

    class Meta:
        model = BookingAdditionalOrder
        fields = [
            "id", "booking", "created_by_id", "accepted_offer_id", "status", "offers", "current_offer",
            "payment", "can_pay", "created_at", "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(AdditionalOrderOfferSerializer(allow_null=True))
    def get_current_offer(self, obj):
        offers = list(obj.offers.all())
        if not offers:
            return None
        offer = max(offers, key=lambda item: item.revision)
        return AdditionalOrderOfferSerializer(offer, context=self.context).data

    @extend_schema_field(AdditionalOrderPaymentSummarySerializer(allow_null=True))
    def get_payment(self, obj):
        try:
            payment = obj.payment
        except ObjectDoesNotExist:
            return None
        return AdditionalOrderPaymentSummarySerializer(payment, context=self.context).data

    def get_can_pay(self, obj) -> bool:
        request = self.context.get("request")
        if not request or obj.status != BookingAdditionalOrder.Status.APPROVED or not obj.accepted_offer_id:
            return False
        if obj.booking.status != "IN_PROGRESS" or obj.booking.customer.user_id != request.user.id:
            return False
        try:
            payment = obj.payment
        except ObjectDoesNotExist:
            payment = None
        return payment is None or payment.status in {"PAYMENT_PENDING", "FAILED"}
