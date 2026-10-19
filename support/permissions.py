from rest_framework.permissions import BasePermission


SUPPORT_AGENT = "CUSTOMER_SUPPORT_AGENT"
SUPPORT_SUPERVISOR = "CUSTOMER_SUPPORT_SUPERVISOR"
SUPPORT_FINANCE = "CUSTOMER_SUPPORT_FINANCE"
SUPPORT_ADMIN = "CUSTOMER_SUPPORT_ADMIN"


ALL_SUPPORT_ROLES = {
    SUPPORT_AGENT,
    SUPPORT_SUPERVISOR,
    SUPPORT_FINANCE,
    SUPPORT_ADMIN,
}


def user_has_support_role(user, roles=None):
    if not user or not user.is_authenticated:
        return False

    if user.is_superuser:
        return True

    if not user.is_staff or not user.is_active:
        return False

    allowed = set(roles or ALL_SUPPORT_ROLES)

    return user.groups.filter(name__in=allowed).exists()


def user_support_roles(user):
    if not user or not user.is_authenticated:
        return []

    return list(
        user.groups.filter(name__in=ALL_SUPPORT_ROLES)
        .values_list("name", flat=True)
    )


class SupportRolePermission(BasePermission):
    allowed_roles = ALL_SUPPORT_ROLES

    def has_permission_for_user(self, user):
        return user_has_support_role(user, self.allowed_roles)

    def has_permission(self, request, view):
        allowed = getattr(
            view,
            "support_roles",
            self.allowed_roles,
        )

        return user_has_support_role(request.user, allowed)


class SupportAgentPermission(SupportRolePermission):
    allowed_roles = {
        SUPPORT_AGENT,
        SUPPORT_SUPERVISOR,
        SUPPORT_ADMIN,
    }


class SupportSupervisorPermission(SupportRolePermission):
    allowed_roles = {
        SUPPORT_SUPERVISOR,
        SUPPORT_ADMIN,
    }


class SupportFinancePermission(SupportRolePermission):
    allowed_roles = {
        SUPPORT_FINANCE,
        SUPPORT_ADMIN,
    }


class SupportAdminPermission(SupportRolePermission):
    allowed_roles = {SUPPORT_ADMIN}