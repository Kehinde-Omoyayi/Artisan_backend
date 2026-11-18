from django.db.models import F
from rest_framework.permissions import BasePermission
from rest_framework.exceptions import PermissionDenied, NotAuthenticated

def revoke_all_sessions(user):
    type(user).objects.filter(pk=user.pk).update(
        token_version=F("token_version") + 1
    )

def is_user_locked(user):
    return user.security_status == "LOCKED"

def locked_response():
    from rest_framework.response import Response
    from rest_framework import status

    return Response(
        {
            "error": "Your account is currently locked. Please contact support.",
            "action": "contact_support",
        },
        status=status.HTTP_403_FORBIDDEN,
    )

def _invalidate_staff_challenges(user):
    from django.utils import timezone
    from .models import StaffSecurityChallenge

    StaffSecurityChallenge.objects.filter(
        user=user,
        consumed_at__isnull=True,
        invalidated_at__isnull=True,
    ).update(invalidated_at=timezone.now())


def suspend_user(user):
    user.is_active = False
    user.security_status = "LOCKED"
    user.save(update_fields=["is_active", "security_status"])
    revoke_all_sessions(user)
    _invalidate_staff_challenges(user)


def unsuspend_user(user):
    user.is_active = True
    user.security_status = "NORMAL"
    user.save(update_fields=["is_active", "security_status"])
    revoke_all_sessions(user)
    _invalidate_staff_challenges(user)

class AnonymousOnly(BasePermission):
    message = "You are already logged in."

    def has_permission(self, request, view):
        if request.user.is_authenticated:
            raise PermissionDenied(
                detail={
                    "error": "You are already logged in.",
                    "action": "redirect_to_home",
                    "redirect_to": "/dashboard/",
                    "requested_url": request.get_full_path(),
                }
            )
        return True

class AuthenticatedOnly(BasePermission):
    message = "Authentication is required."

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            raise NotAuthenticated(
                detail={
                    "error": "Authentication is required.",
                    "action": "redirect_to_login",
                    "next": request.get_full_path(),
                }
            )
        return True
