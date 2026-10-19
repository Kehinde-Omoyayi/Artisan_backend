from rest_framework import serializers

from .models import ArtisanPayoutAccount, EscrowPayment
from support.permissions import SUPPORT_ADMIN, SUPPORT_FINANCE, user_has_support_role

class PaymentSerializer(serializers.ModelSerializer):
    additional_order_id = serializers.UUIDField(source="addon_order_id", read_only=True, allow_null=True)
    service_amount = serializers.SerializerMethodField()
    customer_fee = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    platform_commission = serializers.SerializerMethodField()
    artisan_payout = serializers.SerializerMethodField()
    authorization_url = serializers.SerializerMethodField()

    class Meta:
        model = EscrowPayment
        fields = [
            "id", "booking", "additional_order_id", "reference", "status", "service_amount", "customer_fee",
            "total_amount", "platform_commission", "artisan_payout", "currency", "provider_status",
            "verified_at", "held_at", "release_requested_at", "released_at", "authorization_url", "created_at", "updated_at",
        ]

    currency = serializers.CharField(default="NGN", read_only=True)

    def get_service_amount(self, obj) -> float:
        return obj.service_amount_kobo / 100

    def get_customer_fee(self, obj) -> float:
        return obj.customer_fee_kobo / 100

    def get_total_amount(self, obj) -> float:
        return obj.total_amount_kobo / 100

    def get_platform_commission(self, obj) -> float | None:
        request = self.context.get("request")
        if request and user_has_support_role(request.user, {SUPPORT_FINANCE, SUPPORT_ADMIN}):
            return obj.platform_commission_kobo / 100
        return None

    def get_authorization_url(self, obj) -> str | None:
        request = self.context.get("request")
        if not request or obj.customer_id != request.user.id:
            return None
        payload = obj.provider_payload if isinstance(obj.provider_payload, dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        link = data.get("link")
        return str(link) if link else None

    def get_artisan_payout(self, obj) -> float | None:
        request = self.context.get("request")
        if request and (
            obj.artisan.user_id == request.user.id
            or user_has_support_role(request.user, {SUPPORT_FINANCE, SUPPORT_ADMIN})
        ):
            return obj.artisan_payout_kobo / 100
        return None

class ArtisanPayoutAccountSerializer(serializers.ModelSerializer):
    bank_code = serializers.RegexField(regex=r"^\d{1,10}$", max_length=10)
    account_name = serializers.CharField(max_length=150, trim_whitespace=True)
    account_number = serializers.RegexField(
        regex=r"^\d{10}$",
        max_length=10,
        min_length=10,
        write_only=True,
    )
    bank_name = serializers.CharField(read_only=True)
    account_number_last4 = serializers.CharField(read_only=True)

    class Meta:
        model = ArtisanPayoutAccount
        fields = [
            "bank_code",
            "bank_name",
            "account_number",
            "account_number_last4",
            "account_name",
            "status",
            "is_active",
            "is_verified",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "bank_name",
            "account_number_last4",
            "status",
            "is_active",
            "is_verified",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        submitted = getattr(self, "initial_data", None)
        if submitted is not None:
            allowed = {"account_name", "bank_code", "account_number"}
            unknown = set(submitted.keys()) - allowed
            if unknown:
                raise serializers.ValidationError({
                    key: "Only account name, bank, and account number may be submitted."
                    for key in sorted(unknown)
                })
        if not attrs.get("account_name", "").strip():
            raise serializers.ValidationError({"account_name": "Enter the account holder name."})
        return attrs

    def create(self, validated_data):
        # The raw account number is used for provider verification and immediately
        # encrypted by the view; it is never written to the model in plaintext.
        validated_data.pop("account_number", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("account_number", None)
        return super().update(instance, validated_data)
