from django.core.exceptions import ObjectDoesNotExist
from rest_framework.permissions import BasePermission

from support.permissions import (
    SUPPORT_ADMIN,
    SUPPORT_AGENT,
    SUPPORT_SUPERVISOR,
    user_has_support_role,
)


PROMOTION_REVIEW_ROLES = {
    SUPPORT_AGENT,
    SUPPORT_SUPERVISOR,
    SUPPORT_ADMIN,
}


class IsArtisanApplicant(BasePermission):
    message = "An authenticated artisan account is required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not user.is_active:
            return False
        try:
            return user.artisan_profile is not None
        except (AttributeError, ObjectDoesNotExist):
            return False


class CanReviewPromotionApplications(BasePermission):
    message = "Promotion applications are restricted to authorized Customer Support reviewers."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user_has_support_role(user, PROMOTION_REVIEW_ROLES)
        )
