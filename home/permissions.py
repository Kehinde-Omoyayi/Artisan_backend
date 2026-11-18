from rest_framework.permissions import BasePermission


class IsPlatformSuperuser(BasePermission):
    """Restrict site-wide manager analytics to active Django superusers."""

    message = "Platform manager analytics are restricted to superusers."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.is_superuser
        )
