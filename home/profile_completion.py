"""Profile fields required before customer/artisan messaging or service requests."""

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS

from .phone_numbers import phone_is_valid


PROFILE_FIELD_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "phone_number": "Phone number",
    "country": "Country",
    "state": "State or region",
    "area": "Area or locality",
}
PROFILE_REQUIRED_FIELDS = tuple(PROFILE_FIELD_LABELS)


class ProfileIncomplete(PermissionDenied):
    default_code = "PROFILE_INCOMPLETE"


def _profile_for(user, role):
    relation = "customer_profile" if role == "customer" else "artisan_profile"
    return getattr(user, relation, None)


def profile_role_for(user):
    """Choose a safe default role; service conversations resolve their own context."""
    if _profile_for(user, "artisan") is not None:
        return "artisan"
    if _profile_for(user, "customer") is not None:
        return "customer"
    return "customer" if getattr(user, "api_role", "") == "customer" else "artisan"


def missing_profile_fields(user, *, role=None):
    role = role or profile_role_for(user)
    profile = _profile_for(user, role)
    missing = []

    if not str(getattr(user, "first_name", "") or "").strip():
        missing.append("first_name")
    if not str(getattr(user, "last_name", "") or "").strip():
        missing.append("last_name")
    if not phone_is_valid(getattr(user, "phone_number", None)):
        missing.append("phone_number")

    if profile is None:
        missing.extend(("country", "state", "area"))
        return role, list(dict.fromkeys(missing))

    for field in ("country", "state", "area"):
        if not str(getattr(profile, field, "") or "").strip():
            missing.append(field)
    return role, list(dict.fromkeys(missing))


def require_complete_profile(user, *, role=None):
    resolved_role, missing = missing_profile_fields(user, role=role)
    if missing:
        role_label = "customer" if resolved_role == "customer" else "artisan"
        raise ProfileIncomplete(
            {
                "code": "PROFILE_INCOMPLETE",
                "message": (
                    f"Complete your {role_label} profile before sending a marketplace "
                    "message or submitting a service request."
                ),
                "details": {
                    "profile_role": resolved_role,
                    "missing_fields": missing,
                },
            }
        )
    return True


class RequireCompleteMarketplaceProfile(BasePermission):
    """Apply the profile gate using a service conversation's actual participant role."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        role = None
        conversation_id = getattr(view, "kwargs", {}).get("conversation_id")
        if conversation_id:
            from django.db.models import Q
            from conversations.models import Conversation

            conversation = (
                Conversation.objects.filter(pk=conversation_id)
                .filter(
                    Q(participants=request.user)
                    | Q(customer=request.user)
                    | Q(artisan__user=request.user)
                )
                .select_related("artisan")
                .first()
            )
            if conversation and conversation.conversation_type == Conversation.ConversationType.SERVICE:
                if conversation.customer_id == request.user.id:
                    role = "customer"
                elif conversation.artisan_id and conversation.artisan.user_id == request.user.id:
                    role = "artisan"
        require_complete_profile(request.user, role=role)
        return True


class RequireCompleteCustomerProfile(BasePermission):
    """Require a customer profile before service-request and service-conversation writes."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if not hasattr(request.user, "customer_profile") and hasattr(request.user, "artisan_profile"):
            from home.models import CustomerProfile
            CustomerProfile.objects.get_or_create(user=request.user)
        require_complete_profile(request.user, role="customer")
        return True
