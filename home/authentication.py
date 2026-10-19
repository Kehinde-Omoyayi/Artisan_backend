from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from .auth_tokens import validate_marketplace_token_claims
from .password_policy import (
    is_staff_account,
    staff_password_change_required,
    staff_security_questions_required,
)


class StaffPasswordChangeRequired(PermissionDenied):
    default_detail = "A staff password must be changed before protected work can continue."
    default_code = "PASSWORD_CHANGE_REQUIRED"


class StaffSecuritySetupRequired(PermissionDenied):
    default_detail = "Register all three security questions before accessing the staff workspace."
    default_code = "STAFF_SECURITY_SETUP_REQUIRED"


class StaffAccessRestricted(PermissionDenied):
    default_detail = "Staff accounts may access only explicitly authorized support and manager work."
    default_code = "STAFF_ACCESS_RESTRICTED"


_PASSWORD_MAINTENANCE_PATHS = {
    "/api/v1/change-password/",
    "/api/v1/logout/",
    "/api/v1/forgot-password/",
    "/api/v1/verify-reset-otp/",
    "/api/v1/reset-password/",
    "/api/v1/staff-security-questions/",
    "/api/v1/staff-security-question-options/",
}

_PUBLIC_STAFF_RECOVERY_PREFIXES = (
    "/api/v1/staff-password-resets/",
)


def _is_password_maintenance_path(path):
    return path in _PASSWORD_MAINTENANCE_PATHS or any(
        path.startswith(prefix) for prefix in _PUBLIC_STAFF_RECOVERY_PREFIXES
    )


def _is_staff_work_path_allowed(user, path):
    """Fail closed for staff outside their support role or the manager surface."""
    if _is_password_maintenance_path(path):
        return True

    if user.is_superuser:
        return (
            path.startswith("/api/v1/manager/")
            or path.startswith("/api/v1/support/cases/")
            or path.startswith("/api/v1/promotions/management/")
            or path.startswith("/api/v1/promotions/review/")
            or path.startswith("/api/v1/profile/internal/artisans/")
            or path.startswith("/api/v1/payments/payout-account/")
        )

    from support.permissions import user_has_support_role

    if not user_has_support_role(user):
        return False

    return (
        path.startswith("/api/v1/support/cases/")
        or path.startswith("/api/v1/promotions/review/applications/")
        or path.startswith("/api/v1/promotions/review/campaigns/")
        or path.startswith("/api/v1/promotions/management/campaigns/")
        or path.startswith("/api/v1/profile/internal/artisans/")
        or (
            path.startswith("/api/v1/payments/payout-account/")
            and path.endswith("/verify/")
        )
    )


class MarketplaceJWTAuthentication(JWTAuthentication):
    """Validate token revocation plus the server-side staff access boundary."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)

        token_version = validated_token.get("token_version")

        try:
            token_version_value = int(token_version)
        except (TypeError, ValueError):
            raise InvalidToken("Session has been revoked.") from None

        if token_version_value != user.token_version:
            raise InvalidToken("Session has been revoked.")

        if not user.is_active:
            raise InvalidToken("Account is inactive.")

        if user.security_status == "LOCKED":
            raise InvalidToken("Account is locked.")

        validate_marketplace_token_claims(validated_token, user)
        return user

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, validated_token = result
        if is_staff_account(user):
            path = request.path_info
            if staff_password_change_required(user) and not _is_password_maintenance_path(path):
                raise StaffPasswordChangeRequired()
            if staff_security_questions_required(user) and path not in _PASSWORD_MAINTENANCE_PATHS and not any(
                path.startswith(prefix) for prefix in _PUBLIC_STAFF_RECOVERY_PREFIXES
            ):
                raise StaffSecuritySetupRequired()
            if not _is_staff_work_path_allowed(user, path):
                raise StaffAccessRestricted()

        return user, validated_token
