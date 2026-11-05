from rest_framework import serializers

from .flutterwave import PAYMENT_OPTIONS
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
    checkout = serializers.SerializerMethodField()
    failure_reason = serializers.SerializerMethodField()
    bank_transfer = serializers.SerializerMethodField()
    conversation_id = serializers.SerializerMethodField()
    receipt = serializers.SerializerMethodField()

    class Meta:
        model = EscrowPayment
        fields = [
            "id", "booking", "additional_order_id", "reference", "status", "service_amount", "customer_fee",
            "total_amount", "platform_commission", "artisan_payout", "currency", "provider_status",
            "verified_at", "held_at", "release_requested_at", "released_at", "authorization_url", "checkout", "failure_reason", "bank_transfer", "conversation_id", "receipt", "created_at", "updated_at",
        ]

    currency = serializers.CharField(default="NGN", read_only=True)

    def get_receipt(self, obj) -> dict:
        """The proof of payment, shaped for whoever is asking.

        It exists only once the provider has confirmed the money; until then the
        receipt is an empty object and no screen offers one.
        """
        request = self.context.get("request")
        if not request or not getattr(request, "user", None):
            return {}
        if obj.customer_id == request.user.id:
            viewer = "customer"
        elif getattr(getattr(obj, "artisan", None), "user_id", None) == request.user.id:
            viewer = "artisan"
        else:
            return {}
        from .receipts import build_receipt

        return build_receipt(obj, viewer)

    def get_conversation_id(self, obj) -> str | None:
        """The chat this payment belongs to, so the UI can send the payer back."""
        booking = getattr(obj, "booking", None)
        request = getattr(booking, "request", None)
        conversation = getattr(request, "conversation", None) if request else None
        return str(conversation.id) if conversation else None

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

    def get_checkout(self, obj) -> dict:
        """What the payer can do right now, in plain terms.

        `state` is one of LIVE (open it), EXPIRED (past the provider countdown),
        OLD_METHODS (minted before checkout was restricted to card and bank
        transfer) and NOT_MINTED (nothing was ever created). Only the owner sees
        this; the artisan gets nothing to act on.
        """
        request = self.context.get("request")
        if not request or obj.customer_id != request.user.id:
            return {}
        from .services import checkout_link_is_current, checkout_link_state, checkout_link_ttl_seconds

        state = checkout_link_state(obj)
        if not obj.provider_transaction_id or not state["link"]:
            status_value = "NOT_MINTED"
        elif state["minted_at"] is None:
            status_value = "OLD_METHODS"
        elif state["methods"] != PAYMENT_OPTIONS:
            status_value = "OLD_METHODS"
        elif checkout_link_is_current(obj):
            status_value = "LIVE"
        else:
            status_value = "EXPIRED"
        return {
            "state": status_value,
            "minted_at": state["minted_at"].isoformat() if state["minted_at"] else None,
            "methods": state["methods"],
            "ttl_seconds": checkout_link_ttl_seconds(),
        }

    def get_authorization_url(self, obj) -> str | None:
        """Only a link that is still live and minted for card/bank transfer.

        An expired link, or one minted before checkout was restricted, is hidden
        here so the chat offers "Pay securely in escrow" instead — which mints a
        fresh checkout — rather than sending the payer to a dead PayPal page.
        """
        request = self.context.get("request")
        if not request or obj.customer_id != request.user.id:
            return None
        from .services import checkout_link_state, checkout_link_is_current

        if not checkout_link_is_current(obj):
            return None
        link = checkout_link_state(obj)["link"]
        return link or None

    def get_bank_transfer(self, obj) -> dict:
        """The account this payment is transferred into, for the payer only.

        Shown inside the chat in place of the provider's hosted page: account
        number, bank, the exact amount and when the account expires.
        """
        request = self.context.get("request")
        if not request or obj.customer_id != request.user.id:
            return {}
        if obj.status != EscrowPayment.Status.PAYMENT_PENDING:
            return {}
        from .services import bank_transfer_details, bank_transfer_window

        details = bank_transfer_details(obj)
        if not details["account_number"]:
            return {}
        # One window for the card, the pay button and the renewal guard.
        window = bank_transfer_window(obj)
        return {
            "account_number": details["account_number"],
            "bank": details["bank"],
            "account_name": details["account_name"],
            "amount": details["amount"],
            "note": details["note"],
            "expires_at": details["expires_at"],
            "seconds_left": window["seconds_left"],
            "live": window["live"],
            "live_until": window["until"],
        }

    def get_failure_reason(self, obj) -> str | None:
        """Why the provider refused, for the person who can do something about it.

        When a checkout cannot be minted the payment ends up FAILED with the
        provider's own words in `failure_reason`. Hiding it leaves the payer
        staring at "Payment could not be confirmed" with no way forward, so the
        owner (and support) see it; the artisan does not need it.
        """
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        if obj.customer_id != request.user.id and not user_has_support_role(
            request.user, {SUPPORT_ADMIN, SUPPORT_FINANCE}
        ):
            return None
        return (obj.failure_reason or "").strip() or None

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
    # One-time security code emailed to the artisan; required only when changing
    # an existing payout account. Never stored or returned.
    otp = serializers.RegexField(
        regex=r"^\d{6}$",
        max_length=6,
        min_length=6,
        write_only=True,
        required=False,
        allow_blank=True,
    )
    payout_change_locked_until = serializers.DateTimeField(read_only=True)

    class Meta:
        model = ArtisanPayoutAccount
        fields = [
            "bank_code",
            "bank_name",
            "account_number",
            "account_number_last4",
            "account_name",
            "otp",
            "payout_change_locked_until",
            "status",
            "is_active",
            "is_verified",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "bank_name",
            "account_number_last4",
            "payout_change_locked_until",
            "status",
            "is_active",
            "is_verified",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        submitted = getattr(self, "initial_data", None)
        if submitted is not None:
            allowed = {"account_name", "bank_code", "account_number", "otp"}
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
