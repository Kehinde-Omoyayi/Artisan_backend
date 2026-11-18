from django.contrib import admin

from .models import *


@admin.register(ArtisanPayoutAccount)
class ArtisanPayoutAccountAdmin(admin.ModelAdmin):
    """Read-only view of payout accounts; the artisan owns this state.

    A payout account is only ever created and verified by the artisan through
    ``PUT /payments/payout-account/``, which resolves the account with
    Flutterwave and registers the transfer beneficiary in one step. That flow
    deliberately stores no full account number, so a row written by hand here
    can never be verified: the provider checks would have nothing to re-check,
    and the row still blocks the artisan behind the change-protection code.

    The fields that encode that verified state are therefore read-only, and new
    rows cannot be added from the admin. A stranded row from an earlier manual
    edit is removed with the standard delete action so the artisan can submit
    the details again.
    """

    list_display = [
        "artisan",
        "bank_name",
        "account_number_last4",
        "status",
        "is_verified",
        "is_active",
        "provider_beneficiary_id",
        "payout_change_locked_until",
    ]
    list_filter = ["status", "is_verified", "is_active"]
    search_fields = [
        "artisan__user__email",
        "artisan__business_name",
        "account_name",
        "account_number_last4",
    ]
    readonly_fields = [
        "artisan",
        "bank_code",
        "bank_name",
        "account_number_last4",
        "account_name",
        "account_number_encrypted",
        "provider_beneficiary_id",
        "provider_subaccount_id",
        "status",
        "is_active",
        "is_verified",
        "payout_change_locked_until",
        "created_at",
        "updated_at",
    ]

    def has_add_permission(self, request):
        return False
