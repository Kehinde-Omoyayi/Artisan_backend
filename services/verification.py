from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from .models import ArtisanVerification

def get_artisan_verification(artisan):
    verification, _ = ArtisanVerification.objects.get_or_create(
        artisan=artisan,
        defaults={"verification_status": "UNVERIFIED", "verification_tier": 1},
    )
    return verification

def claim_verification(artisan, method):
    with transaction.atomic():
        verification = get_artisan_verification(artisan)
        verification = ArtisanVerification.objects.select_for_update(of=("self",)).get(pk=verification.pk)

        if artisan.profile_status == "SUSPENDED":
            raise PermissionDenied("Suspended artisans cannot request verification.")

        if (
            verification.verification_status == "VERIFIED"
            and verification.verified_identity_name_encrypted
        ):
            raise PermissionDenied("Your profile is already verified.")

        if verification.verification_status == "PENDING":
            raise PermissionDenied("A verification request is already in progress.")

        if verification.verification_status == "SUSPENDED":
            raise PermissionDenied("Verification is suspended for this profile.")

        legacy_verified_without_name = (
            verification.verification_status == "VERIFIED"
            and not verification.verified_identity_name_encrypted
        )
        if verification.verification_status not in ["UNVERIFIED", "REJECTED"] and not legacy_verified_without_name:
            raise PermissionDenied("Verification cannot be requested for this profile.")

        verification.verification_status = "PENDING"
        verification.verification_method = method
        verification.verification_provider = "mono"
        verification.verification_reference = ""
        verification.verified_identity_name_encrypted = ""
        verification.verified_at = None
        verification.save(update_fields=["verification_status", "verification_method", "verification_provider", "verification_reference", "verified_identity_name_encrypted", "verified_at", "updated_at"])

        return verification

def reset_verification(verification):
    verification.verification_status = "UNVERIFIED"
    verification.verification_method = ""
    verification.verification_reference = ""
    verification.verification_provider = ""
    verification.verified_identity_name_encrypted = ""
    verification.verified_at = None
    verification.save(update_fields=["verification_status", "verification_method", "verification_reference", "verification_provider", "verified_identity_name_encrypted", "verified_at", "updated_at"])

def reject_verification(verification, reference=""):
    verification.verification_status = "REJECTED"
    verification.verification_reference = reference
    verification.verified_identity_name_encrypted = ""
    verification.verified_at = None
    verification.save(update_fields=["verification_status", "verification_reference", "verified_identity_name_encrypted", "verified_at", "updated_at"])

def ensure_pending_bvn(artisan):
    verification = get_artisan_verification(artisan)

    if artisan.profile_status == "SUSPENDED":
        raise PermissionDenied("Suspended artisans cannot continue verification.")

    if verification.verification_status == "VERIFIED":
        raise PermissionDenied("Your profile is already verified.")

    if verification.verification_status == "SUSPENDED":
        raise PermissionDenied("Verification is suspended for this profile.")

    if verification.verification_status != "PENDING" or verification.verification_method != "BVN":
        raise PermissionDenied("No active BVN verification is available.")

    return verification
