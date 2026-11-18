from django.contrib import admin, messages

from .models import *
from .verification import mark_identity_verified_for_admin


@admin.register(ArtisanVerification)
class ArtisanVerificationAdmin(admin.ModelAdmin):
    """Verification rows, with the encrypted identity name kept out of the form.

    ``verified_identity_name_encrypted`` is a Fernet payload that the payout
    identity gate decrypts and compares with the artisan's profile name. Left
    editable it is a trap: an operator can set the status to VERIFIED while the
    payload stays blank, and every payout attempt is then refused with "complete
    identity verification again" even though the admin shows the artisan as
    verified. The supported action below writes a payload that matches the
    profile, which is what the gate requires anyway.
    """

    list_display = [
        "artisan",
        "verification_status",
        "verification_tier",
        "identity_name_available",
        "verified_at",
    ]
    list_filter = ["verification_status", "verification_tier"]
    search_fields = ["artisan__user__email", "artisan__business_name"]
    readonly_fields = ["verified_identity_name_encrypted", "created_at", "updated_at"]
    actions = ["approve_identity_verification"]

    @admin.display(boolean=True, description="Identity name stored")
    def identity_name_available(self, obj):
        return bool(obj.verified_identity_name_encrypted)

    @admin.action(description="Approve identity verification (use the profile name)")
    def approve_identity_verification(self, request, queryset):
        approved = 0
        for verification in queryset.select_related("artisan__user"):
            try:
                mark_identity_verified_for_admin(verification)
            except ValueError as exc:
                self.message_user(
                    request,
                    f"{verification.artisan.user.email}: {exc}",
                    level=messages.ERROR,
                )
                continue
            approved += 1
            self.log_change(
                request,
                verification,
                "Approved identity verification; stored the profile name as the "
                "verified identity.",
            )
        if approved:
            self.message_user(
                request,
                f"Approved {approved} verification(s). Each verified name now "
                "matches that artisan's profile, so payout verification can run.",
                level=messages.SUCCESS,
            )


admin.site.register(ServiceRequest)
