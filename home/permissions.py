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


class IsPlatformOverviewAuthorized(BasePermission):
    """Manager Overview: Super Staff or holders of ``analytics.overview``.

    Superusers satisfy this through the manager layer: they are provisioned as
    Super Administrator holding every catalogue permission, so a single check
    covers both. Finance, support and other ordinary staff roles do not hold
    ``analytics.overview`` and are refused here exactly as before.
    """

    message = "The platform management overview is restricted to authorized managers."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.is_active):
            return False
        try:
            from manager.services.auth import has_perm

            if has_perm(user, "analytics.overview"):
                return True
        except Exception:
            return False
        from .password_policy import is_staff_account

        if is_staff_account(user):
            # Preserve the staff boundary contract: an unauthorized staff
            # account is refused at the boundary, not merely unpermissioned.
            from .authentication import StaffAccessRestricted

            raise StaffAccessRestricted(
                "Staff accounts may access only explicitly authorized support and manager work."
            )
        return False
