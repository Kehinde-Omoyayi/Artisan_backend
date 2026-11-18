"""Shared server-side eligibility gates for promotion application submission and approval."""

from home.profile_completion import PROFILE_FIELD_LABELS, missing_profile_fields


def promotion_eligibility(artisan):
    """Return current promotion eligibility without trusting browser-supplied profile state."""
    failures = []
    if artisan is None:
        return {
            "eligible": False,
            "requirements": [{"code": "ARTISAN_PROFILE_REQUIRED", "message": "An artisan profile is required."}],
            "active_service_count": 0,
        }

    user = artisan.user
    if not user.is_active:
        failures.append({"code": "ACCOUNT_INACTIVE", "message": "Your account must be active."})
    if getattr(user, "security_status", "NORMAL") != "NORMAL":
        failures.append({"code": "ACCOUNT_RESTRICTED", "message": "Resolve the account security restriction before applying."})
    if artisan.profile_status != "ACTIVE":
        failures.append({"code": "PROFILE_NOT_ACTIVE", "message": "Your artisan profile must have Active status."})

    verification = getattr(artisan, "verification", None)
    if verification is None or verification.verification_status != "VERIFIED":
        failures.append({"code": "IDENTITY_NOT_VERIFIED", "message": "Complete successful identity verification before applying."})

    _role, missing = missing_profile_fields(user, role="artisan")
    for field in missing:
        failures.append({
            "code": f"PROFILE_{field.upper()}_MISSING",
            "message": f"Complete your {PROFILE_FIELD_LABELS.get(field, field.replace('_', ' ').title()).lower()} in your profile.",
            "field": field,
        })

    if not str(artisan.business_name or "").strip():
        failures.append({"code": "BUSINESS_NAME_MISSING", "message": "Add your business or professional name to your profile."})
    if not str(artisan.bio or "").strip():
        failures.append({"code": "PROFILE_DESCRIPTION_MISSING", "message": "Add a professional description to your artisan profile."})

    active_service_count = artisan.artisan_services.filter(service__is_active=True).count()
    if active_service_count == 0:
        failures.append({"code": "ACTIVE_SERVICE_REQUIRED", "message": "Add at least one active service to your artisan profile."})

    return {
        "eligible": not failures,
        "requirements": failures,
        "active_service_count": active_service_count,
        "profile_status": artisan.profile_status,
        "verification_status": verification.verification_status if verification is not None else "UNVERIFIED",
    }
