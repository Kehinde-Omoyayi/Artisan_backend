from rest_framework.exceptions import PermissionDenied

def require_customer(conversation, user):
    if conversation.customer_id != user.id: raise PermissionDenied("Only the customer can perform this action.")

def require_artisan(conversation, user):
    if conversation.artisan.user_id != user.id: raise PermissionDenied("Only the assigned artisan can perform this action.")

def require_participant(conversation, user):
    if conversation.customer_id != user.id and conversation.artisan.user_id != user.id: raise PermissionDenied("You are not a participant in this conversation.")
