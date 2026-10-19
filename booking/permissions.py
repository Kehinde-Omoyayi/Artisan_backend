from rest_framework.permissions import BasePermission

class IsBookingParticipant(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.customer.user_id == request.user.id or obj.artisan.user_id == request.user.id
