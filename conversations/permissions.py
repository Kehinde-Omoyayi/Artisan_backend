from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission

from .models import Conversation


class IsConversationParticipant(BasePermission):
    """Allow access only to users belonging to the target conversation."""

    message = "You are not a participant in this conversation."

    def has_permission(self, request, view):
        conversation_id = view.kwargs.get("conversation_id")
        if not conversation_id:
            return bool(request.user and request.user.is_authenticated)
        from django.db.models import Q

        allowed = (
            Conversation.objects.filter(pk=conversation_id)
            .filter(
                Q(participants=request.user)
                | Q(customer=request.user)
                | Q(artisan__user=request.user)
            )
            .exists()
        )
        if not allowed:
            # Identical response for "does not exist" and "not yours": no existence oracle.
            raise NotFound("Conversation not found.")
        return True
