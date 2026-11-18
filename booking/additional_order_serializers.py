from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.pricing import MIN_INDIVIDUAL_PRICE, MIN_INDIVIDUAL_PRICE_MESSAGE

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
    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=MIN_INDIVIDUAL_PRICE,
        error_messages={"min_value": MIN_INDIVIDUAL_PRICE_MESSAGE},
    )
    description = serializers.CharField(max_length=3000, trim_whitespace=True)


class AdditionalOrderResponseSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("accept", "decline", "counter"))
    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=MIN_INDIVIDUAL_PRICE,
        error_messages={"min_value": MIN_INDIVIDUAL_PRICE_MESSAGE},
        required=False,
    )
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
    """Deprecated alias kept for import compatibility.

    The contract is now the full ``PaymentSerializer`` response (see
    ``get_payment``): the add-on payment is the same payment engine as the
    original booking, so it exposes the same fields — including ``checkout``,
    ``bank_transfer`` and the receipt. Bank transfer details remain visible only
    to the payer; the artisan receives nothing actionable.
    """

    class Meta:
        fields = ()



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

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_payment(self, obj):
        try:
            payment = obj.payment
        except ObjectDoesNotExist:
            return None
        from payments.serializers import PaymentSerializer

        return PaymentSerializer(payment, context=self.context).data

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
